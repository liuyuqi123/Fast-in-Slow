"""
train.py

Training script for Vision-Language-Action (VLA) Policies, built on top of pretrained VLMs, trained using mixtures of
the Open-X Embodiment dataset. Performs training in native PyTorch, using Fully-Sharded Data Parallel (FSDP) to run
distributed across GPUs (and nodes). By default, assumes that CUDA toolkit is >= 11.0 (to support BF16 mixed precision).

Notes & Prerequisites:
    - If you want to set a custom location for all HF / TIMM artifacts --> `export HF_HOME="<PATH>"` *before* running!
        => For example (add to end of .bashrc): `export HF_HOME="/mnt/fsx/skaramcheti/cache"`
    - If you want to suppress random Tensorflow logs --> `export TF_CPP_MIN_LOG_LEVEL=3`

Run with:
    - [Single Node One-GPU (Debug)] : torchrun --standalone --nnodes 1 --nproc-per-node 1 vla-scripts/train.py
    - [Single Node Multi-GPU (= $K)]: torchrun --standalone --nnodes 1 --nproc-per-node $K vla-scripts/train.py
"""

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Tuple, Union

import draccus
import torch
import torch.distributed as dist
import yaml

from overwatch import initialize_overwatch
from util import set_global_seed
from vla import get_vla_dataset_and_collator, save_dataset_statistics, ActionTokenizer

from training.metrics import VLAMetrics
from training import get_train_strategy
from conf import VLAConfig, VLARegistry
from models import load, load_vla, load_openvla
from models import FiSvla



from typing import Dict, Optional, Sequence, List
import transformers
# Sane Defaults
os.environ["TOKENIZERS_PARALLELISM"] = "false"


# Initialize Overwatch =>> Wraps `logging.Logger`
overwatch = initialize_overwatch(__name__)


@dataclass
class TrainConfig:
    # fmt: off

    # VLAConfig (`conf/vla.py`); override with --vla.type `VLARegistry.<VLA>.vla_id`
    vla: VLAConfig = field(
        default_factory=VLAConfig.get_choice_class(VLARegistry.EXP_FiSvla_OXE_MAGIC_SOUP_PLUS_MINUS.vla_id)
    )

    # Directory Paths
    data_root_dir: Path = Path(                                     # Path to Open-X dataset directory
        "datasets/open-x-embodiment"
    )
    run_root_dir: Path = Path("runs")                               # Path to directory to store logs & checkpoints

    # Resume Run Parameters
    pretrained_checkpoint: Optional[Union[str, Path]] = None                  # Absolute Path to Checkpoint
    is_resume: bool = True                                          # Whether we are continuing a prior training run
                                                                    # (only applicable given pretrained checkpoint)
    resume_step: Optional[int] = None                               # Global Step to Resume (should match checkpoint)
    resume_epoch: Optional[int] = None                              # Epoch to Resume (should match checkpoint)

    # Run Arguments
    run_id: Optional[str] = None                                    # Run ID for logging, Weights & Biases
    run_id_note: Optional[str] = None                               # Extra note for logging, Weights & Biases
    save_interval: int = 2500                                       # Interval for saving checkpoints (in steps)
    image_aug: bool = False                                         # Whether to enable image augmentations
    seed: int = 42                                                  # Random seed (for reproducibility)

    # HF Hub Credentials (for any gated models)
    hf_token: Union[str, Path] = Path(".hf_token")                  # Environment variable or Path to HF Token

    # Tracking Parameters
    trackers: Tuple[str, ...] = ("jsonl", "wandb")                  # Trackers to initialize (if W&B, add config!)
    #trackers: Tuple[str, ...] = ("jsonl",)                         # Trackers to initialize (if W&B, add config!)
    wandb_project: str = ""                                         # Name of W&B project to log to (use default!)
    wandb_entity: str = ""                                          # Name of entity to log under
    repeated_diffusion_steps: int = 8                               # Repeated steps for training action model (a diffusion model)
    load_all_data_for_training: bool = True                         # Load all training data 
    future_action_window_size: int = 15                             # Action chunking, predicting future actions + current action
    past_action_window_size: int = 0                                # Action history window size, not used now, set to be 0 
    action_dim: int = 7                                             # Dimension of action space
    class_dropout_prob: float = 0.
    action_tokenizer_exist: bool = False
    use_diff: bool = False
    ar_diff_loss: bool = False
    need_to_sub: int = 0
    llm_middle_layer: int = 32
    diffusion_steps: int = 100
    camera_view: str = ""
    fuse: str = 'concat'
    training_mode: str = ''
    load_pointcloud: bool = False
    model_save_num: int = 1
    pointcloud_pos: str = 'slow'
    action_chunk: int = 1
    load_state: bool = True
    lang_subgoals_exist: bool = False

    def __post_init__(self) -> None:
        """Lift optimization parameters from `self.vla` for ease of use =>> validate on `expected_world_size`"""
        self.epochs = self.vla.epochs
        self.max_steps = self.vla.max_steps
        self.global_batch_size = self.vla.global_batch_size
        self.per_device_batch_size = self.vla.per_device_batch_size

        self.learning_rate = self.vla.learning_rate
        self.weight_decay = self.vla.weight_decay
        self.max_grad_norm = self.vla.max_grad_norm
        self.lr_scheduler_type = self.vla.lr_scheduler_type
        self.warmup_ratio = self.vla.warmup_ratio

        self.train_strategy = self.vla.train_strategy

        # [Validate] Assert on `expected_world_size`
        assert (
            self.vla.expected_world_size == overwatch.world_size()
        ), f"Expected World Size = {self.vla.expected_world_size} but Found {overwatch.world_size()} GPUs!"

    # fmt: on


def smart_tokenizer_and_embedding_resize(
    tokenizer: transformers.PreTrainedTokenizer,
    model: transformers.PreTrainedModel,
):
    """Resize tokenizer and embedding.

    Note: This is the unoptimized version that may make your embedding size not be divisible by 64.
    """
    # num_new_tokens = tokenizer.add_special_tokens(special_tokens_dict)
    # model.resize_token_embeddings(len(tokenizer))

    num_new_tokens = tokenizer.add_special_tokens({"additional_special_tokens": ["<BOD>", "<EOD>"]})
    model.resize_token_embeddings(len(tokenizer), pad_to_multiple_of=64)
    if num_new_tokens > 0:
        input_embeddings = model.get_input_embeddings().weight.data
        output_embeddings = model.get_output_embeddings().weight.data

        input_embeddings_avg = input_embeddings[:-num_new_tokens].mean(
            dim=0, keepdim=True)
        output_embeddings_avg = output_embeddings[:-num_new_tokens].mean(
            dim=0, keepdim=True)

        input_embeddings[-num_new_tokens:] = input_embeddings_avg
        output_embeddings[-num_new_tokens:] = output_embeddings_avg

@draccus.wrap()
def train(cfg: TrainConfig) -> None:
    overwatch.info("FiSvla Training :: Warming Up")

    # Note => Under `torchrun` initializing `overwatch` will automatically set up `torch.distributed`
    torch.cuda.set_device(device_id := overwatch.local_rank())
    torch.cuda.empty_cache()

    # Configure Unique Run Name & Save Directory
    vla_id = cfg.vla.vla_id
    cfg.run_id = (
        f"{vla_id}+n{cfg.vla.expected_world_size // 8}+b{cfg.per_device_batch_size}+x{cfg.seed}"
        if cfg.run_id is None
        else cfg.run_id
    )
    if cfg.run_id_note is not None:
        cfg.run_id += f"--{cfg.run_id_note}"
    if cfg.image_aug:
        cfg.run_id += "--image_aug"

    # Start =>> Build Directories and Set Randomness
    overwatch.info('"Do or do not; there is no try."', ctx_level=1)
    hf_token = cfg.hf_token # cfg.hf_token.read_text().strip() if isinstance(cfg.hf_token, Path) else os.environ[cfg.hf_token]
    worker_init_fn = set_global_seed(cfg.seed, get_worker_init_fn=True)
    os.makedirs(run_dir := (cfg.run_root_dir / cfg.run_id), exist_ok=True)
    os.makedirs(cfg.run_root_dir / cfg.run_id / "checkpoints", exist_ok=True)

    # Save Configuration =>> additionally save a JSON version for later HF Integration
    if overwatch.is_rank_zero():
        draccus.dump(cfg, open(run_dir / "config.yaml", "w"))
        with open(run_dir / "config.yaml", "r") as f_yaml, open(run_dir / "config.json", "w") as f_json:
            yaml_cfg = yaml.safe_load(f_yaml)
            json.dump(yaml_cfg, f_json, indent=2)
    
    dist.barrier()
    # Load VLA checkpoint (if resuming from training) or Base VLM otherwise (from `cfg.vla.base_vlm` ID or Path)
    #   =>> Note :: Verifies that all parameters are loaded in FP32 on load!
    overwatch.info(f"Loading Base VLM `{cfg.vla.base_vlm}` from ID/Path")

    if cfg.pretrained_checkpoint is not None and 'openvla' not in cfg.pretrained_checkpoint:
        # [Validate] Pretrained Checkpoint `step` and `epoch` should match `resume_step` and `resume_epoch`
        #   =>> Note :: We make developers pass in `resume_*` arguments as an extra sanity check!
        if cfg.is_resume:
            assert int(re.search("step-(.+?)-", cfg.pretrained_checkpoint.name).group(1)) == cfg.resume_step
            assert int(re.search("epoch-(.+?)-", cfg.pretrained_checkpoint.name).group(1)) == cfg.resume_epoch
        overwatch.info("Loading VLA Checkpoint")
        vla = load_vla(cfg.pretrained_checkpoint, 
                        hf_token=hf_token, 
                        load_for_training=True, 
                        action_dim=cfg.action_dim,
                        future_action_window_size=cfg.future_action_window_size,
                        past_action_window_size=cfg.past_action_window_size,
                        class_dropout_prob=cfg.class_dropout_prob,
                        use_diff=cfg.use_diff,
                        need_to_sub = cfg.need_to_sub,
                        llm_middle_layer=cfg.llm_middle_layer,
                        diffusion_steps = cfg.diffusion_steps,
                        fuse = cfg.fuse,
                        action_tokenizer_exist = cfg.action_tokenizer_exist,
                        training_mode = cfg.training_mode,
                        load_pointcloud = cfg.load_pointcloud,
                        pointcloud_pos = cfg.pointcloud_pos,
                        action_chunk = cfg.action_chunk,
                        load_state = cfg.load_state,
                        lang_subgoals_exist = cfg.lang_subgoals_exist,
                        )
    elif cfg.pretrained_checkpoint is not None and 'openvla' in cfg.pretrained_checkpoint:
        vlm = load_openvla(cfg.pretrained_checkpoint, hf_token=hf_token, load_for_training=True, use_diff=cfg.use_diff, action_dim=cfg.action_dim,llm_middle_layer=cfg.llm_middle_layer,fuse = cfg.fuse,action_tokenizer_exist = cfg.action_tokenizer_exist,training_mode = cfg.training_mode,load_pointcloud = cfg.load_pointcloud,pointcloud_pos = cfg.pointcloud_pos,action_chunk = cfg.action_chunk,load_state = cfg.load_state,)
        overwatch.info("Creating VLA from Base VLM")
        action_tokenizer = ActionTokenizer(vlm.llm_backbone.get_tokenizer(), cfg.need_to_sub)
        vla = FiSvla(vlm, 
                    action_tokenizer,
                    action_dim=cfg.action_dim,
                    future_action_window_size=cfg.future_action_window_size,
                    past_action_window_size=cfg.past_action_window_size,
                    use_diff = cfg.use_diff,
                    diffusion_steps = cfg.diffusion_steps,
                    load_pointcloud = cfg.load_pointcloud,
                    action_chunk=cfg.action_chunk,
                    load_state = cfg.load_state,
                    lang_subgoals_exist=cfg.lang_subgoals_exist,
                    )
        # del this variable to avoid bugs. The vlm shouldn't be used anymore
        del vlm

    else:
        vlm = load(cfg.vla.base_vlm, hf_token=hf_token, load_for_training=True, use_diff=cfg.use_diff, action_dim=cfg.action_dim,llm_middle_layer=cfg.llm_middle_layer,fuse = cfg.fuse,action_tokenizer_exist = cfg.action_tokenizer_exist,training_mode = cfg.training_mode,load_pointcloud = cfg.load_pointcloud,pointcloud_pos = cfg.pointcloud_pos,action_chunk = cfg.action_chunk,load_state = cfg.load_state,)
        overwatch.info("Creating VLA from Base VLM")
        action_tokenizer = ActionTokenizer(vlm.llm_backbone.get_tokenizer(), cfg.need_to_sub)
        vla = FiSvla(vlm, 
                    action_tokenizer,
                    action_dim=cfg.action_dim,
                    future_action_window_size=cfg.future_action_window_size,
                    past_action_window_size=cfg.past_action_window_size,
                    use_diff = cfg.use_diff,
                    diffusion_steps = cfg.diffusion_steps,
                    load_pointcloud = cfg.load_pointcloud,
                    action_chunk=cfg.action_chunk,
                    load_state = cfg.load_state,
                    lang_subgoals_exist=cfg.lang_subgoals_exist,
                    )
        # del this variable to avoid bugs. The vlm shouldn't be used anymore
        del vlm

    smart_tokenizer_and_embedding_resize(tokenizer = vla.llm_backbone.get_tokenizer(), model = vla.llm_backbone.llm)

    # [Validate] Model should be in Full Precision!
    for param in vla.parameters():
        assert param.dtype == torch.float32, f"Loaded VLM parameter not in full precision: {param}"

    # Determine training "stage" based on frozen vs unfrozen parameters --> supports different fine-tuning schemes!
    if not cfg.vla.freeze_vision_backbone and not cfg.vla.freeze_llm_backbone:
        stage = "full-finetune"  # Full fine-tuning
    elif cfg.vla.freeze_vision_backbone and not cfg.vla.freeze_llm_backbone:
        stage = "finetune"  # Frozen vision encoder
    elif cfg.vla.freeze_vision_backbone and cfg.vla.freeze_llm_backbone:
        stage = "align"  # Fine-tuning projector
    elif not cfg.vla.freeze_vision_backbone and cfg.vla.freeze_llm_backbone and cfg.vla.unfreeze_last_llm_layer:
        stage = "vla-sandwich-train"  # Fine-tuning vision encoder, projector, and LLM last layer
    elif cfg.vla.freeze_vision_backbone and cfg.vla.freeze_llm_backbone and cfg.vla.unfreeze_last_llm_layer:
        stage = "vla-last-layer-train"  # Fine-tuning LLM last layer only
    else:
        raise ValueError(
            "Weight freezing configuration not supported. VLA config has the following parameters: "
            f"freeze_vision_backbone: {cfg.vla.freeze_vision_backbone}"
            f"freeze_llm_backbone: {cfg.vla.freeze_llm_backbone}"
            f"unfreeze_last_llm_layer: {cfg.vla.unfreeze_last_llm_layer}"
        )

    # [Explicit] Call to `freeze_backbones` here for clarity =>> will log exactly what is/is not frozen
    overwatch.info(f"Invoking `VLM.freeze_backbones()` for `{vla_id}` => Stage: `{stage}`")
    vla.freeze_backbones(stage)

    if cfg.vla.freeze_vision_backbone and cfg.vla.freeze_llm_backbone:
        for name, param in vla.vlm.named_parameters():
            param.requires_grad = False

    # Print number of total/trainable model parameters
    num_params = sum(p.numel() for p in vla.parameters())
    num_trainable_params = sum(p.numel() for p in vla.parameters() if p.requires_grad)
    overwatch.info(
        f"# Parameters (in millions): {num_params / 10**6:.3f} Total, {num_trainable_params / 10**6:.3f} Trainable"
    )

    overwatch.info(f"Creating VLA Open-X Dataset with Mixture `{cfg.vla.data_mix}`")
    vla_dataset, _, collator = get_vla_dataset_and_collator(
        cfg.data_root_dir,
        cfg.vla.data_mix,
        image_transform=vla.vision_backbone.get_image_transform(),
        tokenizer=vla.llm_backbone.get_tokenizer(),
        prompt_builder_fn=vla.llm_backbone.prompt_builder_fn,
        default_image_resolution=vla.vision_backbone.default_image_resolution,
        shuffle_buffer_size=cfg.vla.shuffle_buffer_size,
        image_aug=cfg.image_aug,
        load_all_data_for_training=cfg.load_all_data_for_training,
        future_action_window_size=cfg.future_action_window_size,
        past_action_window_size=cfg.past_action_window_size,
        action_tokenizer_exist=cfg.action_tokenizer_exist,
        need_to_sub = cfg.need_to_sub,
        camera_view=cfg.camera_view,
        load_pointcloud = cfg.load_pointcloud,
        action_chunk = cfg.action_chunk,
        lang_subgoals_exist = cfg.lang_subgoals_exist,
    )

    # Save dataset statistics for de-normalization at inference time
    if overwatch.is_rank_zero():
        save_dataset_statistics(vla_dataset.dataset_statistics, run_dir)
    
    dist.barrier()
    # Create Train Strategy
    overwatch.info(f"Initializing Train Strategy `{cfg.train_strategy}`")
    train_strategy = get_train_strategy(
        train_strategy=cfg.train_strategy,
        vlm=vla,
        device_id=device_id,
        stage=stage,
        epochs=cfg.epochs,
        max_steps=cfg.max_steps,
        global_batch_size=cfg.global_batch_size,
        per_device_batch_size=cfg.per_device_batch_size,
        learning_rate=cfg.learning_rate,
        weight_decay=cfg.weight_decay,
        max_grad_norm=cfg.max_grad_norm,
        lr_scheduler_type=cfg.lr_scheduler_type,
        warmup_ratio=cfg.warmup_ratio,
        enable_gradient_checkpointing=cfg.vla.enable_gradient_checkpointing,
        enable_mixed_precision_training=cfg.vla.enable_mixed_precision_training,
        reduce_in_full_precision=cfg.vla.reduce_in_full_precision,
        worker_init_fn=worker_init_fn,
    )
    train_strategy.run_setup(run_dir=run_dir, n_train_examples=len(vla_dataset))
    if cfg.pretrained_checkpoint is not None and cfg.is_resume:
        train_strategy.load_optimizer_and_scheduler(cfg.pretrained_checkpoint)

    # Create Metrics =>> Handles on the fly tracking, logging to specified trackers (e.g., JSONL, Weights & Biases)
    overwatch.info(f"Creating Metrics with Active Trackers => `{cfg.trackers}`")
    metrics = VLAMetrics(
        cfg.trackers,
        cfg.run_id,
        run_dir,
        draccus.encode(cfg),
        wandb_project=cfg.wandb_project,
        wandb_entity=cfg.wandb_entity,
        resume_step=cfg.resume_step,
        resume_epoch=cfg.resume_epoch,
    )

    # Run VLA Training
    overwatch.info("Starting VLA Training Loop")
    train_strategy.run_vla_training(
        vla_dataset,
        collator,
        metrics,
        save_interval=cfg.save_interval,
        use_diff=cfg.use_diff,
        repeated_diffusion_steps = cfg.repeated_diffusion_steps,
        ar_diff_loss = cfg.ar_diff_loss,
        model_save_num = cfg.model_save_num,
    )

    # Finalize
    overwatch.info("Done with Training =>> Finalizing Metrics")
    metrics.finalize()

    # And... we're done!
    overwatch.info("... and that's all, folks!")
    dist.barrier()
    dist.destroy_process_group()


if __name__ == "__main__":
    train()
