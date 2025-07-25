"""
fisvla.py

"""

from __future__ import annotations
import time
from functools import partial
from pathlib import Path
from typing import Callable, Dict, List, Optional, Type, Union, Tuple
from copy import deepcopy

import torch
import torch.nn as nn
import numpy as np
from PIL import Image
from torch.distributed.fsdp.wrap import _module_wrap_policy, _or_policy
from torch.nn.utils.rnn import pad_sequence
from transformers import LlamaTokenizerFast

from models.backbones import LLMBackbone, VisionBackbone
from models.vlms import PrismaticVLM
from models.diffusion import create_diffusion
from util import FusedMLPProjector, LinearProjector, MLPProjector
from models.diffusion import ActionEmbedder, TimestepEmbedder, LabelEmbedder, FinalLayer
from overwatch import initialize_overwatch
from vla import ActionTokenizer

from models.vlms.pointcloud_processor.lift3d_dino import Lift3dDinov2

# Initialize Overwatch =>> Wraps `logging.Logger`
overwatch = initialize_overwatch(__name__)

# HuggingFace Default / LLaMa-2 IGNORE_INDEX (for labels)
IGNORE_INDEX = -100


class FiSvla(nn.Module):
    def __init__(
        self,
        vlm: PrismaticVLM,
        action_tokenizer: ActionTokenizer,
        token_size: int = 4096,
        action_dim: int = 7,
        future_action_window_size: int = 15,
        past_action_window_size: int = 0,
        norm_stats: Dict[str, Dict[str, Dict[str, Dict[str, List[float]]]]] = None,
        use_diff: bool = False,
        diffusion_steps: int = 100,
        load_pointcloud: bool = False,
        action_chunk: int = 1,
        load_state: bool = True,
        action_tokenizer_exist: bool = False,
        lang_subgoals_exist: bool = False,
        **kwargs,
    ) -> None:
        super().__init__()

        self.action_tokenizer = action_tokenizer
        self.load_pointcloud = load_pointcloud
        self.action_chunk = action_chunk
        self.load_state = load_state
        self.action_tokenizer_exist = action_tokenizer_exist
        self.lang_subgoals_exist = lang_subgoals_exist

        self.use_diff = use_diff
        self.vlm = vlm
        self.future_action_window_size = future_action_window_size
        self.vlm.future_action_window_size = future_action_window_size
        self.past_action_window_size = past_action_window_size
        self.all_module_keys=[]
        for module_keys in self.vlm.all_module_keys:
            self.all_module_keys.append("vlm." + module_keys)
        self.norm_stats = norm_stats
        self._trainable_module_keys = []

        if self.use_diff:
            self.ddim_diffusion = None
            self.diffusion_steps = diffusion_steps
            self.diffusion = create_diffusion(timestep_respacing="", noise_schedule = 'squaredcos_cap_v2', diffusion_steps=self.diffusion_steps, sigma_small=True, learn_sigma = False)

    @property
    def trainable_module_keys(self) -> List[str]:
        keys = []
        for module_keys in self.vlm.trainable_module_keys:
            keys.append("vlm." + module_keys)
        keys += self._trainable_module_keys
        return keys
    
    @property
    def llm_backbone(self) -> LLMBackbone:
        return self.vlm.llm_backbone
    
    @property
    def vision_backbone(self) -> VisionBackbone:
        return self.vlm.vision_backbone
    
    def freeze_backbones(self, stage):
        self.vlm.freeze_backbones(stage)

    def _repeat_tensor(
        self, 
        tensor: Optional[torch.Tensor], 
        repeated_diffusion_steps: int
    ) -> Optional[torch.Tensor]:
        """
        Repeat a tensor along the first dimension

        Args:
            tensor: Input tensor to repeat
            repeated_diffusion_steps: Number of times to repeat

        Returns:
            Repeated tensor or None
        """
        if tensor is None:
            return None
        return tensor.repeat(repeated_diffusion_steps, *([1] * (tensor.ndimension() - 1)))

    def _repeat_pixel_values(
        self, 
        pixel_values: Union[torch.Tensor, Dict[str, torch.Tensor], None], 
        repeated_diffusion_steps: int
    ) -> Union[torch.Tensor, Dict[str, torch.Tensor], None]:
        """
        Repeat pixel values, handling different input types

        Args:
            pixel_values: Pixel values (tensor or dict)
            repeated_diffusion_steps: Number of times to repeat

        Returns:
            Repeated pixel values
        """
        if pixel_values is None:
            return None

        if isinstance(pixel_values, torch.Tensor):
            return pixel_values.repeat(repeated_diffusion_steps, *([1] * (pixel_values.ndimension() - 1)))
        
        if isinstance(pixel_values, dict):
            return {
                key: value.repeat(repeated_diffusion_steps, *([1] * (value.ndimension() - 1)))
                for key, value in pixel_values.items()
            }
        
        raise ValueError(f"Unsupported `pixel_values` type = {type(pixel_values)}")


    def forward(
        self,
        input_ids: Optional[torch.LongTensor] = None,
        attention_mask: Optional[torch.Tensor] = None,
        pixel_values: Optional[torch.FloatTensor] = None,
        point_cloud: Optional[torch.FloatTensor] = None,
        labels: Optional[torch.LongTensor] = None,
        actions: Optional[torch.FloatTensor] = None,
        proprio: Optional[torch.FloatTensor] = None,
        inputs_embeds: Optional[torch.FloatTensor] = None,
        past_key_values: Optional[List[torch.FloatTensor]] = None,
        use_cache: Optional[bool] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
        repeated_diffusion_steps: int = 3,
        action_masks = None,
        use_diff: Optional[bool] = None,
    ) -> Tuple:
        """
        Forward pass through the Diffusion-based Vision-Language Model

        Args:
            Multiple input parameters for VLM and diffusion process
            repeated_diffusion_steps: Number of times to repeat inputs
            use_diff: Flag to enable diffusion mode

        Returns:
            Loss and output in diffusion mode, or just output in normal mode
        """
        # Update diffusion mode flag
        if use_diff is not None:
            self.use_diff = use_diff
        
        # Repeat proprio inputs
        if self.load_state:
            proprio = self._repeat_tensor(proprio, repeated_diffusion_steps)

        # Diffusion-specific processing
        if self.use_diff:
            # Repeat inputs
            actions = self._repeat_tensor(actions, repeated_diffusion_steps)
            input_ids = self._repeat_tensor(input_ids, repeated_diffusion_steps)
            attention_mask = self._repeat_tensor(attention_mask, repeated_diffusion_steps)
            action_masks = self._repeat_tensor(action_masks, repeated_diffusion_steps)
            labels = self._repeat_tensor(labels, repeated_diffusion_steps)
            
            # Repeat pixel values with type-safe handling
            pixel_values = self._repeat_pixel_values(pixel_values, repeated_diffusion_steps)

            if point_cloud is not None:
                point_cloud = self._repeat_tensor(point_cloud, repeated_diffusion_steps)

            # Generate noise and timesteps for diffusion
            noise = torch.randn_like(actions)  # [B, T, C]
            timestep = torch.randint(
                0, 
                self.diffusion.num_timesteps, 
                (actions.size(0),), 
                device=actions.device
            )
            
            # Apply diffusion sampling
            x = self.diffusion.q_sample(actions, timestep, noise)

            # Run VLM with diffusion
            output, noise_pred = self.vlm(
                input_ids=input_ids,
                attention_mask=attention_mask,
                pixel_values=pixel_values,
                point_cloud=point_cloud,
                labels=labels,
                x=x,
                t=timestep,
                proprio=proprio,
                inputs_embeds=inputs_embeds,
                past_key_values=past_key_values,
                use_cache=use_cache,
                output_attentions=output_attentions,
                output_hidden_states=output_hidden_states,
                return_dict=return_dict,
                use_diff=self.use_diff,
            )

            # Compute loss
            assert noise_pred.shape == noise.shape == actions.shape
            loss = ((noise_pred - noise) ** 2).mean()
            return loss, output

        # Non-diffusion mode
        else:
            output = self.vlm(
                input_ids=input_ids,
                attention_mask=attention_mask,
                pixel_values=pixel_values,
                point_cloud=point_cloud,
                labels=labels,
                proprio=proprio,
                inputs_embeds=inputs_embeds,
                past_key_values=past_key_values,
                use_cache=use_cache,
                output_attentions=output_attentions,
                output_hidden_states=output_hidden_states,
                return_dict=return_dict,
                use_diff=self.use_diff,
            )
            return output
        

    def get_fsdp_wrapping_policy(self) -> Callable:
        """Return an FSDP _or_policy over the policies returned by each individual backbone (and our VLM policy)."""
        vision_fsdp_wrapping_policy = self.vlm.vision_backbone.get_fsdp_wrapping_policy()
        llm_fsdp_wrapping_policy = self.vlm.llm_backbone.get_fsdp_wrapping_policy()
        # Get Prismatic Wrapping Policy =>> just a module wrapping policy around `self.projector` and DiT
        prismatic_fsdp_wrapping_policy = partial(
            _module_wrap_policy,
            module_classes={LinearProjector, MLPProjector, FusedMLPProjector, ActionEmbedder, TimestepEmbedder, LabelEmbedder, FinalLayer},
        )

        # Return union (_or_) over constituent policies
        #   => Note: there is *not* a fall-through policy; any module that isn't covered by the above constituents will
        #            automatically be folded into the root VLM FSDP instance.
        policies=[
            vision_fsdp_wrapping_policy,
            llm_fsdp_wrapping_policy,
            prismatic_fsdp_wrapping_policy,
        ]

        if self.load_pointcloud:
            pointcloud_fsdp_wrapping_policy = self.vlm.pointcloud_backbone.get_fsdp_wrapping_policy()
            policies.append(pointcloud_fsdp_wrapping_policy)

        return partial(
            _or_policy,
            policies=policies,
        )

    @classmethod
    def from_pretrained(
        cls,
        pretrained_checkpoint: Path,
        model_id: str,
        vision_backbone: VisionBackbone,
        llm_backbone: LLMBackbone,
        pointcloud_backbone: Optional[Lift3dDinov2] = None,
        enable_mixed_precision_training: bool = True,
        arch_specifier: str = "gelu-mlp",
        freeze_weights: bool = True,
        action_dim: int = 7,
        future_action_window_size: int = 15,
        past_action_window_size: int = 0,
        norm_stats = None,
        class_dropout_prob: float = 0.0,
        need_to_sub: int = 0,
        use_diff: bool = False,
        llm_middle_layer: int = 32,
        diffusion_steps: int = 100,
        action_tokenizer_exist: bool = False,
        training_mode: str = "",
        load_pointcloud: bool = False,
        pointcloud_pos: str = "slow",
        action_chunk: int = 1,
        load_state: bool = True,
        lang_subgoals_exist: bool = False,
        **kwargs,
    ) -> FiSvla:

        # Load VLM backbone, borrowed from PrismaticVLM
        vlm = PrismaticVLM(
            model_id,
            vision_backbone,
            llm_backbone,
            pointcloud_backbone=pointcloud_backbone,
            enable_mixed_precision_training=enable_mixed_precision_training,
            arch_specifier=arch_specifier,
            class_dropout_prob=class_dropout_prob,
            use_diff=use_diff,
            action_dim=action_dim,
            token_size=llm_backbone.embed_dim,
            llm_middle_layer=llm_middle_layer,
            action_tokenizer_exist=action_tokenizer_exist,
            training_mode=training_mode,
            load_pointcloud=load_pointcloud,
            pointcloud_pos=pointcloud_pos,
            action_chunk=action_chunk,
            load_state=load_state,
            **kwargs,
        )
        
        # Load action tokenizer from llm
        action_tokenizer = ActionTokenizer(llm_backbone.get_tokenizer(), need_to_sub)

        # Load from Checkpoint (Custom --> should load both *projector* and *llm* weights)
        model_state_dict = torch.load(pretrained_checkpoint, map_location="cpu")["model"]

        if "vision_backbone" in model_state_dict.keys():
            vlm.vision_backbone.load_state_dict(model_state_dict["vision_backbone"])
        else:
            raise ValueError("no vision backbone found!")

        assert (
            "projector" in model_state_dict and "llm_backbone" in model_state_dict
        ), "PrismaticVLM `from_pretrained` expects checkpoint with keys for `projector` AND `llm_backbone`!"
        vlm.projector.load_state_dict(model_state_dict["projector"])
        vlm.llm_backbone.load_state_dict(model_state_dict["llm_backbone"])

        if load_state and "proprio_embedder" in model_state_dict.keys() and model_state_dict["proprio_embedder"]["mlp.fc1.weight"].shape[-1] == action_dim:
            vlm.proprio_embedder.load_state_dict(model_state_dict["proprio_embedder"])
            print("load proprio_layer!!!!")
        else:
            print("no load proprio_layer!!!!")

        if "x_embedder" in model_state_dict.keys() and "t_embedder" in model_state_dict.keys() and "final_layer" in model_state_dict.keys() and use_diff:
            if model_state_dict["x_embedder"]["mlp.fc1.weight"].shape[-1] == action_dim:
                vlm.x_embedder.load_state_dict(model_state_dict["x_embedder"])
                vlm.t_embedder.load_state_dict(model_state_dict["t_embedder"])
                vlm.final_layer.load_state_dict(model_state_dict["final_layer"])
                print("load x_t_final_layer!!!!")
            else:
                print("no x_t_final_layer!!!!")

        if load_pointcloud and "pointcloud_backbone" in model_state_dict.keys() and "projector_3d_dino" in model_state_dict.keys() and "projector_3d_siglip" in model_state_dict.keys():
            vlm.pointcloud_backbone.load_state_dict(model_state_dict["pointcloud_backbone"])
            vlm.projector_3d_dino.load_state_dict(model_state_dict["projector_3d_dino"])
            vlm.projector_3d_siglip.load_state_dict(model_state_dict["projector_3d_siglip"])
            print("load pointcloud backbone and projector_3d !!!!")
        else:
            print("no pointcloud backbone and projector_3d !!!!")

        # Freeze Weights
        if freeze_weights:
            vlm.requires_grad_(False)
            vlm.eval()

        # Initialize FiSvla
        fisvla = FiSvla(vlm,
                        action_tokenizer,
                        token_size = vlm.llm_backbone.llm.lm_head.in_features,
                        action_dim = action_dim,
                        future_action_window_size = future_action_window_size,
                        past_action_window_size = past_action_window_size,
                        norm_stats = norm_stats,
                        use_diff=use_diff,
                        diffusion_steps = diffusion_steps,
                        load_pointcloud = load_pointcloud,
                        action_chunk=action_chunk,
                        load_state=load_state,
                        action_tokenizer_exist=action_tokenizer_exist,
                        lang_subgoals_exist=lang_subgoals_exist,
                        )

        return fisvla        

    @torch.inference_mode()
    def predict_action(
        self, 
        image_head_slow :Optional[Image] = None,
        image_head_fast :Optional[Image] = None, 
        image_right_slow :Optional[Image] = None,
        image_right_fast :Optional[Image] = None,
        image_left_slow :Optional[Image] = None,
        image_left_fast :Optional[Image] = None,
        point_cloud = None,
        instruction: str = "", 
        unnorm_key: Optional[str] = None, 
        cfg_scale: float = 1.5, 
        use_ddim: bool = False,
        num_ddim_steps: int = 5,
        action_dim: int = 7,
        cur_robot_state: Optional[str] = None,
        multi_view: bool = True,
        predict_mode: str = "diff+ar",
        **kwargs: str
    ):
        """
        Core function for VLA inference; maps input image and task instruction to continuous action.

        @param image: PIL Image as [height, width, 3]
        @param instruction: Task instruction string
        @param unnorm_key: Optional dataset name for retrieving un-normalizing statistics; if None, checks that model
                           was trained only on a single dataset, and retrieves those statistics.
        @param cfg_scale: Scaling factor for classifier-free guidance (CFG); if == 1.0, CFG is disabled.
        @param use_ddim: Use DDIM sampling instead of DDPM sampling.
        @param num_ddim_steps: Number of DDIM steps to use for sampling.

        @return Unnormalized (continuous) action vector --> end-effector deltas.
        """

        image_transform, tokenizer = self.vlm.vision_backbone.image_transform, self.vlm.llm_backbone.tokenizer
        device = self.vlm.device
        autocast_dtype = self.vlm.llm_backbone.half_precision_dtype
        
        message = f"What action should the robot take to {instruction.lower()}?"
        prompt_builder = self.vlm.get_prompt_builder()
        prompt_builder.add_turn(role="human", message=message)
        prompt_text = prompt_builder.get_prompt()
        
        input_ids = tokenizer(prompt_text, truncation=True, return_tensors="pt").input_ids.to(device)
        
        if not isinstance(tokenizer, LlamaTokenizerFast):
            raise ValueError(f"Unsupported `tokenizer` type = {type(tokenizer)}")
        
        def append_tokens(ids_to_append):
            token_tensor = torch.tensor([ids_to_append], dtype=torch.long, device=device)
            return torch.cat((input_ids, token_tensor), dim=1)
        
        has_empty_token = lambda: torch.all(input_ids[:, -1] == 29871)
        
        if self.vlm.model_id == 'prism-dinosiglip-224px+7b':
            if not has_empty_token():
                input_ids = append_tokens([29871, 32001, 32002, 29871])
        elif self.vlm.model_id == 'phi-2+3b':
            input_ids = append_tokens([220, 50296, 50297])
        else:
            raise ValueError(f"Unsupported predict_mode = {predict_mode}")
        
        pixel_values = {}
        def process_image(image, prefix):
            if image is None:
                return
            
            pv = image_transform(image)
            if isinstance(pv, torch.Tensor):
                pv = pv[None, ...].to(device)
            elif isinstance(pv, dict):
                pv = {k: v[None, ...].to(device) for k, v in pv.items()}
            else:
                raise ValueError(f"Unsupported `{prefix}_pixel_values` type = {type(pv)}")
            
            for key, value in pv.items():
                pixel_values[f"{prefix}_{key}"] = value
        
        process_image(image_head_slow, "head_slow")
        process_image(image_head_fast, "head_fast")
        process_image(image_right_slow, "right_slow")
        process_image(image_right_fast, "right_fast")
        process_image(image_left_slow, "left_slow")
        process_image(image_left_fast, "left_fast")

        if point_cloud is not None:
            point_cloud = torch.tensor(point_cloud, dtype=torch.float32).unsqueeze(0).to(device)
        
        if cur_robot_state is not None:
            proprio_norm_stats = self.get_proprio_stats(unnorm_key)
            mask = proprio_norm_stats.get("mask", np.ones_like(proprio_norm_stats["q01"], dtype=bool))
            proprio_high, proprio_low = np.array(proprio_norm_stats["q99"]), np.array(proprio_norm_stats["q01"])
            cur_robot_state = np.where(
                mask,
                2 * (cur_robot_state - proprio_low) / (proprio_high - proprio_low + 1e-8) - 1,
                cur_robot_state,
            )
            cur_robot_state = np.clip(cur_robot_state, -1, 1)
            cur_robot_state = torch.tensor(cur_robot_state, dtype=torch.float32).unsqueeze(0).unsqueeze(0).to(device)
        
        def unnormalize_actions(normalized_actions):
            action_norm_stats = self.get_action_stats(unnorm_key)
            mask = action_norm_stats.get("mask", np.ones_like(action_norm_stats["q01"], dtype=bool))
            action_high, action_low = np.array(action_norm_stats["q99"]), np.array(action_norm_stats["q01"])

            mask = np.array(mask).reshape(normalized_actions.shape[0], action_dim)
            action_high = action_high.reshape(normalized_actions.shape[0], action_dim)
            action_low = action_low.reshape(normalized_actions.shape[0], action_dim)
            
            normalized_actions = np.clip(normalized_actions, -1, 1)
            
            if isinstance(normalized_actions, np.ndarray):
                if normalized_actions.ndim == 1 and len(normalized_actions) == 7:
                    normalized_actions[6] = np.where(normalized_actions[6] < 0.5, 0, 1)
                elif normalized_actions.ndim == 1 and len(normalized_actions) == 14:
                    normalized_actions[6] = np.where(normalized_actions[6] < 0.5, 0, 1)
                    normalized_actions[13] = np.where(normalized_actions[13] < 0.5, 0, 1)
                elif normalized_actions.ndim > 1:
                    if normalized_actions.shape[1] == 7:
                        normalized_actions[:, 6] = np.where(normalized_actions[:, 6] < 0.5, 0, 1)
                    elif normalized_actions.shape[1] == 14:
                        normalized_actions[:, 6] = np.where(normalized_actions[:, 6] < 0.5, 0, 1)
                        normalized_actions[:, 13] = np.where(normalized_actions[:, 13] < 0.5, 0, 1)
            
            actions = np.where(
                mask,
                0.5 * (normalized_actions + 1) * (action_high - action_low) + action_low,
                normalized_actions,
            )
            return actions
        
        def prepare_diffusion(input_ids_diff=None):
            noise = torch.randn(1, self.action_chunk, action_dim, device=device)
            timestep = torch.randint(0, self.diffusion.num_timesteps, (1,), device=device)
            using_cfg = cfg_scale > 1.0
            
            if input_ids_diff is None:
                input_ids_diff = input_ids
                if self.vlm.model_id == 'prism-dinosiglip-224px+7b':
                    input_ids_diff = input_ids_diff[:, :-2]
                elif self.vlm.model_id == 'phi-2+3b':
                    input_ids_diff = input_ids_diff[:, :-1]
            
            if using_cfg:
                noise = torch.cat([noise, noise], 0)
                uncondition = self.vlm.z_embedder.uncondition.unsqueeze(0).expand(input_ids_diff.shape[0], 1, -1)
                sample_fn = self.vlm.forward_with_cfg
                model_kwargs = {
                    'slow_llm_embeddings': uncondition, 
                    'cfg_scale': cfg_scale, 
                    'input_ids': input_ids_diff, 
                    'pixel_values': pixel_values,
                }
                if cur_robot_state is not None:
                    model_kwargs['proprio'] = cur_robot_state
                if point_cloud is not None:
                    model_kwargs['point_cloud'] = point_cloud
            else:
                model_kwargs = {'input_ids': input_ids_diff, 'pixel_values': pixel_values}
                if cur_robot_state is not None:
                    model_kwargs['proprio'] = cur_robot_state
                if point_cloud is not None:
                    model_kwargs['point_cloud'] = point_cloud
                sample_fn = self.vlm.forward
            
            return noise, timestep, sample_fn, model_kwargs, using_cfg
        
        def sample_diffusion(noise, sample_fn, model_kwargs, using_cfg):
            if use_ddim and num_ddim_steps is not None:
                if self.ddim_diffusion is None:
                    self.create_ddim(ddim_step=num_ddim_steps)
                samples = self.ddim_diffusion.ddim_sample_loop(
                    sample_fn, 
                    noise.shape, 
                    noise, 
                    clip_denoised=False,
                    model_kwargs=model_kwargs,
                    progress=False,
                    device=device,
                    eta=0.0
                )
            else:
                samples = self.diffusion.p_sample_loop(
                    sample_fn, 
                    noise.shape, 
                    noise, 
                    clip_denoised=False,
                    model_kwargs=model_kwargs,
                    progress=False,
                    device=device
                )
            
            if using_cfg:
                samples, _ = samples.chunk(2, dim=0)  
            
            return samples[0].cpu().numpy()
        
        def predict_diff():
            with torch.autocast("cuda", dtype=autocast_dtype, enabled=self.vlm.enable_mixed_precision_training):
                noise, timestep, sample_fn, model_kwargs, using_cfg = prepare_diffusion()
                normalized_actions = sample_diffusion(noise, sample_fn, model_kwargs, using_cfg)
            return unnormalize_actions(normalized_actions)
        
        def predict_ar():
            with torch.autocast("cuda", dtype=autocast_dtype, enabled=self.vlm.enable_mixed_precision_training):
                noise = torch.randn(1, self.action_chunk, action_dim, device=device)
                timestep = torch.randint(0, self.diffusion.num_timesteps, (1,), device=device)
                
                outputs = super(PrismaticVLM, self.vlm).generate(
                    x=noise,
                    proprio=cur_robot_state,
                    t=timestep,
                    input_ids=input_ids,
                    pixel_values=pixel_values,
                    point_cloud=point_cloud,
                    max_new_tokens=100,
                    gen_discret_action=False,
                    ar_infer=True,
                    output_scores=True,
                    return_dict_in_generate=True,
                    **kwargs
                )
            
            # logits = outputs.scores
            # probs = [torch.softmax(log, dim=-1) for log in logits]
            # last_n_tensors = probs[-action_dim:]
            # max_probs = [tensor.max().item() for tensor in last_n_tensors]
            
            generated_ids = outputs.sequences[:, input_ids.shape[1]:]
            
            if self.action_tokenizer_exist and self.lang_subgoals_exist:
                predicted_action_token_ids = generated_ids[0, :self.get_action_dim(unnorm_key)]
                normalized_actions = self.action_tokenizer.decode_token_ids_to_actions(predicted_action_token_ids.cpu().numpy())
                normalized_actions = np.expand_dims(normalized_actions, axis=0)
                actions = unnormalize_actions(normalized_actions)
                predicted_subgoals_token_ids = generated_ids[0, self.get_action_dim(unnorm_key):-1]
                predicted_language_subgoals = self.vlm.llm_backbone.tokenizer.decode(predicted_subgoals_token_ids.cpu().numpy())
                return actions, predicted_language_subgoals
            elif not self.action_tokenizer_exist and self.lang_subgoals_exist:
                predicted_subgoals_token_ids = generated_ids[0, :-1]
                predicted_language_subgoals = self.vlm.llm_backbone.tokenizer.decode(predicted_subgoals_token_ids.cpu().numpy())
                return None, predicted_language_subgoals
            elif self.action_tokenizer_exist and not self.lang_subgoals_exist:
                predicted_action_token_ids = generated_ids[0, :self.get_action_dim(unnorm_key)]
                normalized_actions = self.action_tokenizer.decode_token_ids_to_actions(predicted_action_token_ids.cpu().numpy())
                normalized_actions = np.expand_dims(normalized_actions, axis=0)
                actions = unnormalize_actions(normalized_actions)
                return actions, None
            else:
                return None,None

        if predict_mode == 'diff':
            actions = predict_diff()
            return actions
        
        elif predict_mode == 'ar':
            actions, predicted_language_subgoals = predict_ar()
            return actions, predicted_language_subgoals
        
        elif predict_mode == 'diff+ar':
            actions_ar, predicted_language_subgoals = predict_ar()
            actions_diff = predict_diff()
            return actions_diff, actions_ar, predicted_language_subgoals



    @torch.inference_mode()
    def slow_system_forward(
        self, 
        image_head_slow :Optional[Image] = None,
        image_right_slow :Optional[Image] = None,
        image_left_slow :Optional[Image] = None,
        point_cloud = None,
        instruction: str = "", 
        unnorm_key: Optional[str] = None, 
        cur_robot_state: Optional[str] = None,
        **kwargs: str
    ):

        image_transform, tokenizer = self.vlm.vision_backbone.image_transform, self.vlm.llm_backbone.tokenizer
        device = self.vlm.device
        autocast_dtype = self.vlm.llm_backbone.half_precision_dtype
        
        message = f"What action should the robot take to {instruction.lower()}?"
        prompt_builder = self.vlm.get_prompt_builder()
        prompt_builder.add_turn(role="human", message=message)
        prompt_text = prompt_builder.get_prompt()
        
        input_ids = tokenizer(prompt_text, truncation=True, return_tensors="pt").input_ids.to(device)
        
        if not isinstance(tokenizer, LlamaTokenizerFast):
            raise ValueError(f"Unsupported `tokenizer` type = {type(tokenizer)}")
        
        def append_tokens(ids_to_append):
            token_tensor = torch.tensor([ids_to_append], dtype=torch.long, device=device)
            return torch.cat((input_ids, token_tensor), dim=1)
        
        has_empty_token = lambda: torch.all(input_ids[:, -1] == 29871)
        
        if self.vlm.model_id == 'prism-dinosiglip-224px+7b':
            if not has_empty_token():
                input_ids = append_tokens([29871, 32001])
        elif self.vlm.model_id == 'phi-2+3b':
            input_ids = append_tokens([220, 50296])
        else:
            raise ValueError(f"Unsupported predict_mode")
        
        pixel_values = {}
        def process_image(image, prefix):
            if image is None:
                return
            
            pv = image_transform(image)
            if isinstance(pv, torch.Tensor):
                pv = pv[None, ...].to(device)
            elif isinstance(pv, dict):
                pv = {k: v[None, ...].to(device) for k, v in pv.items()}
            else:
                raise ValueError(f"Unsupported `{prefix}_pixel_values` type = {type(pv)}")
            
            for key, value in pv.items():
                pixel_values[f"{prefix}_{key}"] = value
        
        process_image(image_head_slow, "head_slow")
        process_image(image_right_slow, "right_slow")
        process_image(image_left_slow, "left_slow")

        if point_cloud is not None:
            point_cloud = torch.tensor(point_cloud, dtype=torch.float32).unsqueeze(0).to(device)
        
        if cur_robot_state is not None:
            proprio_norm_stats = self.get_proprio_stats(unnorm_key)
            mask = proprio_norm_stats.get("mask", np.ones_like(proprio_norm_stats["q01"], dtype=bool))
            proprio_high, proprio_low = np.array(proprio_norm_stats["q99"]), np.array(proprio_norm_stats["q01"])
            cur_robot_state = np.where(
                mask,
                2 * (cur_robot_state - proprio_low) / (proprio_high - proprio_low + 1e-8) - 1,
                cur_robot_state,
            )
            cur_robot_state = np.clip(cur_robot_state, -1, 1)
            cur_robot_state = torch.tensor(cur_robot_state, dtype=torch.float32).unsqueeze(0).unsqueeze(0).to(device)
        
        with torch.autocast("cuda", dtype=autocast_dtype, enabled=self.vlm.enable_mixed_precision_training):
            output = self.vlm.slow_system_infer_forward(
                input_ids=input_ids,
                pixel_values=pixel_values,
                point_cloud=point_cloud,
                gen_discret_action=False,
            )

        return input_ids, output



    @torch.inference_mode()
    def fast_system_forward(
        self, 
        image_head_fast :Optional[Image] = None, 
        image_right_fast :Optional[Image] = None,
        image_left_fast :Optional[Image] = None,
        slow_latent_embedding = None,
        input_ids = None,
        point_cloud = None,
        unnorm_key: Optional[str] = None, 
        cfg_scale: float = 1.5, 
        use_ddim: bool = False,
        num_ddim_steps: int = 5,
        action_dim: int = 7,
        cur_robot_state: Optional[str] = None,
        predict_mode: str = "diff",
        **kwargs: str
    ):
        """
        Core function for VLA inference; maps input image and task instruction to continuous action.

        @param image: PIL Image as [height, width, 3]
        @param instruction: Task instruction string
        @param unnorm_key: Optional dataset name for retrieving un-normalizing statistics; if None, checks that model
                           was trained only on a single dataset, and retrieves those statistics.
        @param cfg_scale: Scaling factor for classifier-free guidance (CFG); if == 1.0, CFG is disabled.
        @param use_ddim: Use DDIM sampling instead of DDPM sampling.
        @param num_ddim_steps: Number of DDIM steps to use for sampling.

        @return Unnormalized (continuous) action vector --> end-effector deltas.
        """

        image_transform, tokenizer = self.vlm.vision_backbone.image_transform, self.vlm.llm_backbone.tokenizer
        device = self.vlm.device
        autocast_dtype = self.vlm.llm_backbone.half_precision_dtype
        
        pixel_values = {}
        def process_image(image, prefix):
            if image is None:
                return
            
            pv = image_transform(image)
            if isinstance(pv, torch.Tensor):
                pv = pv[None, ...].to(device)
            elif isinstance(pv, dict):
                pv = {k: v[None, ...].to(device) for k, v in pv.items()}
            else:
                raise ValueError(f"Unsupported `{prefix}_pixel_values` type = {type(pv)}")
            
            for key, value in pv.items():
                pixel_values[f"{prefix}_{key}"] = value
        
        process_image(image_head_fast, "head_fast")
        process_image(image_right_fast, "right_fast")
        process_image(image_left_fast, "left_fast")

        if point_cloud is not None:
            point_cloud = torch.tensor(point_cloud, dtype=torch.float32).unsqueeze(0).to(device)
        
        if cur_robot_state is not None:
            proprio_norm_stats = self.get_proprio_stats(unnorm_key)
            mask = proprio_norm_stats.get("mask", np.ones_like(proprio_norm_stats["q01"], dtype=bool))
            proprio_high, proprio_low = np.array(proprio_norm_stats["q99"]), np.array(proprio_norm_stats["q01"])
            cur_robot_state = np.where(
                mask,
                2 * (cur_robot_state - proprio_low) / (proprio_high - proprio_low + 1e-8) - 1,
                cur_robot_state,
            )
            cur_robot_state = np.clip(cur_robot_state, -1, 1)
            cur_robot_state = torch.tensor(cur_robot_state, dtype=torch.float32).unsqueeze(0).unsqueeze(0).to(device)
        
        def unnormalize_actions(normalized_actions):
            action_norm_stats = self.get_action_stats(unnorm_key)
            mask = action_norm_stats.get("mask", np.ones_like(action_norm_stats["q01"], dtype=bool))
            action_high, action_low = np.array(action_norm_stats["q99"]), np.array(action_norm_stats["q01"])

            mask = np.array(mask).reshape(normalized_actions.shape[0], action_dim)
            action_high = action_high.reshape(normalized_actions.shape[0], action_dim)
            action_low = action_low.reshape(normalized_actions.shape[0], action_dim)
            
            normalized_actions = np.clip(normalized_actions, -1, 1)
            
            if isinstance(normalized_actions, np.ndarray):
                if normalized_actions.ndim == 1 and len(normalized_actions) == 7:
                    normalized_actions[6] = np.where(normalized_actions[6] < 0.5, 0, 1)
                elif normalized_actions.ndim == 1 and len(normalized_actions) == 14:
                    normalized_actions[6] = np.where(normalized_actions[6] < 0.5, 0, 1)
                    normalized_actions[13] = np.where(normalized_actions[13] < 0.5, 0, 1)
                elif normalized_actions.ndim > 1:
                    if normalized_actions.shape[1] == 7:
                        normalized_actions[:, 6] = np.where(normalized_actions[:, 6] < 0.5, 0, 1)
                    elif normalized_actions.shape[1] == 14:
                        normalized_actions[:, 6] = np.where(normalized_actions[:, 6] < 0.5, 0, 1)
                        normalized_actions[:, 13] = np.where(normalized_actions[:, 13] < 0.5, 0, 1)
            
            actions = np.where(
                mask,
                0.5 * (normalized_actions + 1) * (action_high - action_low) + action_low,
                normalized_actions,
            )
            return actions
        
        def prepare_diffusion():
            noise = torch.randn(1, self.action_chunk, action_dim, device=device)
            using_cfg = cfg_scale > 1.0
            
            model_kwargs = {'input_ids': input_ids, 'slow_latent_embedding': slow_latent_embedding, 'pixel_values': pixel_values}
            if cur_robot_state is not None:
                model_kwargs['proprio'] = cur_robot_state
            if point_cloud is not None:
                model_kwargs['point_cloud'] = point_cloud
            sample_fn = self.vlm.fast_system_infer_forward
            
            return noise, sample_fn, model_kwargs, using_cfg
        
        def sample_diffusion(noise, sample_fn, model_kwargs, using_cfg):
            if use_ddim and num_ddim_steps is not None:
                if self.ddim_diffusion is None:
                    self.create_ddim(ddim_step=num_ddim_steps)
                samples = self.ddim_diffusion.ddim_sample_loop(
                    sample_fn, 
                    noise.shape, 
                    noise, 
                    clip_denoised=False,
                    model_kwargs=model_kwargs,
                    progress=False,
                    device=device,
                    eta=0.0
                )
            else:
                samples = self.diffusion.p_sample_loop(
                    sample_fn, 
                    noise.shape, 
                    noise, 
                    clip_denoised=False,
                    model_kwargs=model_kwargs,
                    progress=False,
                    device=device
                )
            
            if using_cfg:
                samples, _ = samples.chunk(2, dim=0)  
            
            return samples[0].cpu().numpy()
        
        def predict_diff():
            with torch.autocast("cuda", dtype=autocast_dtype, enabled=self.vlm.enable_mixed_precision_training):
                noise, sample_fn, model_kwargs, using_cfg = prepare_diffusion()
                normalized_actions = sample_diffusion(noise, sample_fn, model_kwargs, using_cfg)
            return unnormalize_actions(normalized_actions)
        
        if predict_mode == 'diff':
            actions = predict_diff()
            return actions

    @torch.inference_mode()
    def predict_action_batch(
        self, image: List[Image], 
        instruction: List[str], 
        unnorm_key: Optional[str] = None, 
        cfg_scale: float = 1.5, 
        use_ddim: bool = False,
        num_ddim_steps: int = 10,
        **kwargs: str
    ) -> np.ndarray:
        """
        Core function for VLA inference in batch; maps input image and task instruction to continuous action.
        This function is used for batch inference in the simulators.
        @param image: PIL Image as [height, width, 3]
        @param instruction: Task instruction string
        @param unnorm_key: Optional dataset name for retrieving un-normalizing statistics; if None, checks that model
                           was trained only on a single dataset, and retrieves those statistics.
        @param cfg_scale: Scaling factor for classifier-free guidance (CFG); if == 1.0, CFG is disabled.
        @param use_ddim: Use DDIM sampling instead of DDPM sampling.
        @param num_ddim_steps: Number of DDIM steps to use for sampling.

        @return Unnormalized (continuous) action vector --> end-effector deltas.
        """
        image_transform, tokenizer = self.vlm.vision_backbone.image_transform, self.vlm.llm_backbone.tokenizer
        
        input_ids = []
        pixel_values = []

        # Build VLA Prompt
        B = len(image)

        if isinstance(tokenizer, LlamaTokenizerFast):
            pass
        else:
            raise ValueError(f"Unsupported `tokenizer` type = {type(tokenizer)}")

        for id in range(B):
            prompt_builder = self.vlm.get_prompt_builder()
            prompt_builder.add_turn(role="human", message=f"What action should the robot take to {instruction[id].lower()}?")
            prompt_text = prompt_builder.get_prompt()
            # Prepare Inputs
            single_input_ids = tokenizer(prompt_text, truncation=True, return_tensors="pt").input_ids.to(self.vlm.device).squeeze(0)
            # Note: We need to add this special empty token ('') after the colon (':') token in "ASSISTANT:"
            #       insert it to match the inputs seen at training time. The empty token is at index 29871.
            #       We also need to add the special cognition token at index 2 (i.e. the EOS token).
            single_input_ids = torch.cat(
                (single_input_ids, torch.Tensor([29871, 2]).long().to(self.vlm.device)), dim=0
            ) # [seq]

            input_ids.append(single_input_ids)
            # Preprocess Image
            pixel_values.append(image_transform(image[id]))

        # Padding
        padding_side = "right"
        # For now, we only support Tokenizers with `padding_side = "right"`
        #   => Handle padding via RNN Utils => `pad_sequence`
        assert padding_side == "right", f"Invalid Tokenizer `{padding_side = }`"

        model_max_length = tokenizer.model_max_length
        pad_token_id = tokenizer.pad_token_id
        input_ids = pad_sequence(input_ids, batch_first=True, padding_value=pad_token_id)

        # Truncate (if necessary)
        input_ids = input_ids[:, : model_max_length]
        # Get `attention_mask` by checking for `pad_token_id`
        attention_mask = input_ids.ne(pad_token_id)

        # Preprocess Image
        if isinstance(pixel_values[0], torch.Tensor):
            pixel_values = torch.stack(pixel_values).to(self.vlm.device)
        elif isinstance(pixel_values[0], dict):
            pixel_values = {
                k: torch.stack([pixel_values[idx][k] for idx in range(len(input_ids))]).to(self.vlm.device) for k in pixel_values[0]
            }
        else:
            raise ValueError(f"Unsupported `pixel_values` type = {type(pixel_values)}")

        # Invoke super().generate --> taps into `GenerationMixin` which (redirects) to `forward()`
        autocast_dtype = self.vlm.llm_backbone.half_precision_dtype
        with torch.autocast("cuda", dtype=autocast_dtype, enabled=self.vlm.enable_mixed_precision_training):
            # fmt: off
            output = super(PrismaticVLM, self.vlm).generate(
                input_ids=input_ids,                            # Shape: [1, seq]
                pixel_values=pixel_values,                      # Shape: [1, 3, res, res] or Dict[str, ...]
                max_new_tokens=1,
                output_hidden_states=True, 
                return_dict_in_generate=True,
                attention_mask = attention_mask,
                **kwargs
            )
            # fmt: on

        # Extract cognition feature
        if self.vlm.vision_backbone.featurizer is not None:
            num_patch = self.vlm.vision_backbone.featurizer.patch_embed.num_patches
        elif hasattr(self.vlm.vision_backbone, 'siglip_featurizer') and self.vlm.vision_backbone.siglip_featurizer is not None:
            num_patch = self.vlm.vision_backbone.siglip_featurizer.patch_embed.num_patches
        else:
            raise ValueError("No vision backbone found")

        last_hidden = output.hidden_states[0][-1]
        last_hidden = last_hidden[:, num_patch :]

        cumulative_sum = attention_mask.cumsum(dim=1)  
        last_true_indices = (cumulative_sum == cumulative_sum.max(dim=1, keepdim=True)[0]).float().argmax(dim=1)  
        expanded_indices = last_true_indices.unsqueeze(-1).expand(-1, last_hidden.size(-1))  
        cognition_features = last_hidden.gather(1, expanded_indices.unsqueeze(1)).squeeze(1) #[B, D]

        assert (cognition_features.shape[0], cognition_features.shape[1]) == (B, 4096), "Batch size must be B for action prediction"
        using_cfg = cfg_scale > 1.0


        model_dtype = next(self.action_model.net.parameters()).dtype

        B = cognition_features.shape[0]
        
        cognition_features = cognition_features.unsqueeze(1).to(model_dtype)  # [B, 1, D]

        # Sample random noise
        noise = torch.randn(B, self.action_chunk, self.action_model.in_channels, device=cognition_features.device).to(model_dtype)  #[B, T, D]
        # Setup classifier-free guidance:
        if using_cfg:
            noise = torch.cat([noise, noise], 0)
            uncondition = self.action_model.net.z_embedder.uncondition
            uncondition = uncondition.unsqueeze(0)  #[1, D]
            uncondition = uncondition.expand(B, 1, -1) #[B, 1, D]
            z = torch.cat([cognition_features, uncondition], 0)
            cfg_scale = cfg_scale
            model_kwargs = dict(z=z, cfg_scale=cfg_scale)
            sample_fn = self.action_model.net.forward_with_cfg
        else:
            model_kwargs = dict(z=cognition_features)
            sample_fn = self.action_model.net.forward

        # DDIM Sampling
        if use_ddim and num_ddim_steps is not None:
            if self.action_model.ddim_diffusion is None:
                self.action_model.create_ddim(ddim_step=num_ddim_steps)
            samples = self.action_model.ddim_diffusion.ddim_sample_loop(sample_fn, 
                                                                noise.shape, 
                                                                noise, 
                                                                clip_denoised=False,#False, try to set True 
                                                                model_kwargs=model_kwargs,
                                                                progress=False,
                                                                device=cognition_features.device,
                                                                eta=0.0)
        else:
            # DDPM Sampling
            samples = self.action_model.diffusion.p_sample_loop(sample_fn, 
                                                                    noise.shape, 
                                                                    noise, 
                                                                    clip_denoised=False,#False, try to set True 
                                                                    model_kwargs=model_kwargs,
                                                                    progress=False,
                                                                    device=cognition_features.device)
        if using_cfg:
            samples, _ = samples.chunk(2, dim=0)  # Remove null class samples
        normalized_actions = samples.cpu().numpy()

        # Un-normalize Actions
        action_norm_stats = self.get_action_stats(unnorm_key)
        mask = action_norm_stats.get("mask", np.ones_like(action_norm_stats["q01"], dtype=bool))
        action_high, action_low = np.array(action_norm_stats["q99"]), np.array(action_norm_stats["q01"])
        normalized_actions = np.clip(normalized_actions, -1, 1)
        normalized_actions[:, :, 6] = np.where(normalized_actions[:, :, 6] < 0.5, 0, 1) 
        actions = np.where(
            mask,
            0.5 * (normalized_actions + 1) * (action_high - action_low) + action_low,
            normalized_actions,
        )
        return actions, normalized_actions
    
    def create_ddim(self, ddim_step=10, noise_schedule = 'squaredcos_cap_v2', diffusion_steps = 100):
        self.ddim_diffusion = create_diffusion(timestep_respacing = "ddim"+str(ddim_step), 
                                               noise_schedule = noise_schedule,
                                               diffusion_steps = diffusion_steps, 
                                               sigma_small = True, 
                                               learn_sigma = False
                                               )
        return self.ddim_diffusion

    @staticmethod
    def _check_unnorm_key(norm_stats, unnorm_key):
        if unnorm_key is None:
            assert len(norm_stats) == 1, (
                f"Your model was trained on more than one dataset, "
                f"please pass a `unnorm_key` from the following options to choose the statistics "
                f"used for un-normalizing actions: {norm_stats.keys()}"
            )
            unnorm_key = next(iter(norm_stats.keys()))

        assert unnorm_key in norm_stats, (
            f"The `unnorm_key` you chose is not in the set of available dataset statistics, "
            f"please choose from: {norm_stats.keys()}"
        )
        return unnorm_key

    def get_action_dim(self, unnorm_key=None):
        """Dimensionality of the policy's action space."""
        unnorm_key = self._check_unnorm_key(self.norm_stats, unnorm_key)
        return len(self.norm_stats[unnorm_key]["action"]["q01"])

    def get_proprio_stats(self, unnorm_key=None):
        """Dimensionality of the policy's proprio space."""
        unnorm_key = self._check_unnorm_key(self.norm_stats, unnorm_key)
        return self.norm_stats[unnorm_key]["proprio"]
    
    def get_action_stats(self, unnorm_key=None):
        """Dimensionality of the policy's action space."""
        unnorm_key = self._check_unnorm_key(self.norm_stats, unnorm_key)
        return self.norm_stats[unnorm_key]["action"]

