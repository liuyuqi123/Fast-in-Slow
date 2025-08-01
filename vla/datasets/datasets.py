"""
datasets.py

Lightweight PyTorch Dataset Definition for wrapping RLDS TFDS Pipeline; just defines transform from RLDS default
format to OpenVLA, IterableDataset shim.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Tuple, Type, Optional

import numpy as np
import torch
from PIL import Image
from torch.utils.data import Dataset, IterableDataset
from transformers import PreTrainedTokenizerBase

from models.backbones.llm.prompting import PromptBuilder
from models.backbones.vision import ImageTransform
from util.data_utils import tree_map
from vla.action_tokenizer import ActionTokenizer
from vla.datasets.rlds import make_interleaved_dataset, make_single_dataset
from vla.datasets.rlds.oxe import OXE_NAMED_MIXTURES, get_oxe_dataset_kwargs_and_weights
from vla.datasets.rlds.utils.data_utils import NormalizationType

# HuggingFace Default / LLaMa-2 IGNORE_INDEX (for labels)
IGNORE_INDEX = -100


@dataclass
class RLDSBatchTransform:
    action_tokenizer: ActionTokenizer
    base_tokenizer: PreTrainedTokenizerBase
    image_transform: ImageTransform
    prompt_builder_fn: Type[PromptBuilder]
    predict_stop_token: bool = True
    load_pointcloud: bool = False
    action_chunk: int = 1
    lang_subgoals_exist: bool = False

    def __call__(self, rlds_batch: Dict[str, Any]) -> Dict[str, Any]:
        """Converts a RLDS batch to the format expected by the OpenVLA collator/models."""

        dataset_name, action, proprio = rlds_batch["dataset_name"], rlds_batch["action"], rlds_batch["observation"]["proprio"]
        lang = rlds_batch["task"]["language_instruction"].decode().lower()

        lang_subgoals = ""
        if self.lang_subgoals_exist is True and 'language_subgoals' in rlds_batch["task"]:
            lang_subgoals = rlds_batch["task"]["language_subgoals"].decode().lower()
            lang_subgoals = f"Now, you need to do the following operation: {lang_subgoals}."

        # If action tokenizer is not used, we don't add the action to the chat answer
        if self.action_tokenizer is None and self.lang_subgoals_exist is None:
            conversation = [
                {"from": "human", "value": f"What action should the robot take to {lang}?"},
                {"from": "gpt", "value": "<BOD><EOD>"},
            ]
        elif self.action_tokenizer is not None and self.lang_subgoals_exist is None:
            gpt_values = ""
            for act in action:
                gpt_values += self.action_tokenizer(act[:len(proprio[0])])
            # cur_robot_state = ""
            # for pro in proprio:
            #     cur_robot_state += self.action_tokenizer(pro)
            ##  The current robot state is {cur_robot_state}. 
            conversation = [
                {"from": "human", "value": f"What action should the robot take to {lang}?"},
                {"from": "gpt", "value": f"<BOD><EOD>{gpt_values}"},
            ]
        elif self.action_tokenizer is None and self.lang_subgoals_exist is not None:
            conversation = [
                {"from": "human", "value": f"What action should the robot take to {lang}?"},
                {"from": "gpt", "value": f"<BOD><EOD>{lang_subgoals}"},
            ]
        else:
            gpt_values = ""
            for act in action:
                gpt_values += self.action_tokenizer(act[:len(proprio[0])])
            # cur_robot_state = ""
            # for pro in proprio:
            #     cur_robot_state += self.action_tokenizer(pro)
            ##  The current robot state is {cur_robot_state}. 
            conversation = [
                {"from": "human", "value": f"What action should the robot take to {lang}?"},
                {"from": "gpt", "value": f"<BOD><EOD>{gpt_values}{lang_subgoals}."},
            ]

        # Construct Chat-based Prompt
        prompt_builder = self.prompt_builder_fn("openvla")
        for turn in conversation:
            prompt_builder.add_turn(turn["from"], turn["value"])
        # Tokenize (w/ `base_tokenizer`)
        input_ids = self.base_tokenizer(prompt_builder.get_prompt(), add_special_tokens=True).input_ids

        # if self.action_tokenizer is not None:
        #     last_index = len(input_ids) - 1 - input_ids[::-1].index(29871) if 29871 in input_ids else None
        #     if last_index is not None:
        #         input_ids.pop(last_index)

        labels = list(input_ids)
        # Tensorize =>> Run Image Transform to get `pixel_values` =>> Return
        #   =>> IMPORTANT :: IF WE'RE USING HF LLM.forward(..., labels=labels), SHIFTING HAPPENS _INSIDE_ MODEL!
        input_ids, labels  = torch.tensor(input_ids), torch.tensor(labels)

        images = {
            "head_slow": None,
            "head_fast": None,
            "right_slow": None,
            "right_fast": None,
            "left_slow": None,
            "left_fast": None,
        }
        key_mapping = {
            "image_head_slow": "head_slow",
            "image_head_fast": "head_fast",
            "image_right_slow": "right_slow",
            "image_right_fast": "right_fast",
            "image_left_slow": "left_slow",
            "image_left_fast": "left_fast",
        }

        for obs_key, prefix in key_mapping.items():
            if obs_key in rlds_batch["observation"]:
                images[prefix] = Image.fromarray(rlds_batch["observation"][obs_key][0])
        pixel_values = {}
        for prefix, img in images.items():
            if img is not None:
                transformed = self.image_transform(img)
                for key, value in transformed.items():
                    pixel_values[f"{prefix}_{key}"] = value

        point_cloud = None
        if self.load_pointcloud and "pointcloud" in rlds_batch["observation"]:
            point_cloud = rlds_batch["observation"]["pointcloud"][0]  # (1024, 3)
            point_cloud = torch.tensor(point_cloud, dtype=torch.float32)

        action_mask = None
        action = torch.tensor(action, dtype=torch.float32).reshape(self.action_chunk, -1)
        proprio = torch.tensor(proprio, dtype=torch.float32)
        if "action_mask" in rlds_batch:
            action_mask = torch.tensor(rlds_batch["action_mask"], dtype=torch.bool)

        if self.action_tokenizer is None and self.lang_subgoals_exist is None:
            labels[:] = IGNORE_INDEX
        else:
            matches = (labels == 32002).nonzero().view(-1)
            last_position = matches[-1].item() if len(matches) > 0 else None
            labels[: last_position+2] = IGNORE_INDEX   ##  last_position is the position of 32002, then +1 is because 29871

        if not self.predict_stop_token:
            labels[-1] = IGNORE_INDEX

        return dict(pixel_values=pixel_values, pointcloud=point_cloud, input_ids=input_ids, labels=labels, dataset_name=dataset_name, actions=action, action_masks=action_mask, proprio = proprio)


class RLDSDataset(IterableDataset):
    def __init__(
        self,
        data_root_dir: Path,
        data_mix: str,
        batch_transform: RLDSBatchTransform,
        resize_resolution: Tuple[int, int],
        shuffle_buffer_size: int = 256_000,
        future_action_window_size: int = 0,
        past_action_window_size: int = 0,
        train: bool = True,
        image_aug: bool = False,
        load_all_data_for_training: bool = True,
        camera_view: Optional[str] = "primary",
        load_pointcloud: bool = False,
        action_chunk : int = 1,
    ) -> None:
        """Lightweight wrapper around RLDS TFDS Pipeline for use with PyTorch/OpenVLA Data Loaders."""
        self.data_root_dir, self.data_mix, self.batch_transform = data_root_dir, data_mix, batch_transform

        # Configure RLDS Dataset(s)
        if self.data_mix in OXE_NAMED_MIXTURES:
            mixture_spec = OXE_NAMED_MIXTURES[self.data_mix]
        else:
            # Assume that passed "mixture" name is actually a single dataset -- create single-dataset "mix"
            mixture_spec = [(self.data_mix, 1.0)]

        camera_view = tuple(camera_view.split(","))

        # fmt: off
        per_dataset_kwargs, weights = get_oxe_dataset_kwargs_and_weights(
            self.data_root_dir,
            mixture_spec,
            load_camera_views=camera_view,
            load_depth=False,
            load_proprio=False,
            load_language=True,
            load_pointcloud=load_pointcloud,
            action_proprio_normalization_type=NormalizationType.BOUNDS_Q99,
            action_chunk = action_chunk,
        )
        rlds_config = dict(
            traj_transform_kwargs=dict(
                window_size=past_action_window_size + 1,                                    # If we wanted to feed / predict more than one step
                future_action_window_size=future_action_window_size,                        # For action chunking
                skip_unlabeled=True,                                                        # Skip trajectories without language labels
                #goal_relabeling_strategy="uniform",                                        # Goals are currently unused
            ),
            frame_transform_kwargs=dict(
                resize_size=resize_resolution,
                num_parallel_calls=16,                          # For CPU-intensive ops (decoding, resizing, etc.)
            ),
            dataset_kwargs_list=per_dataset_kwargs,
            shuffle_buffer_size=shuffle_buffer_size,
            sample_weights=weights,
            balance_weights=True,
            traj_transform_threads=len(mixture_spec),
            traj_read_threads=len(mixture_spec),
            train=train,
            load_all_data_for_training=load_all_data_for_training,
        )

        # If applicable, enable image augmentations
        if image_aug:
            rlds_config["frame_transform_kwargs"].update({"image_augment_kwargs" : dict(
                random_resized_crop=dict(scale=[0.9, 0.9], ratio=[1.0, 1.0]),
                random_brightness=[0.2],
                random_contrast=[0.8, 1.2],
                random_saturation=[0.8, 1.2],
                random_hue=[0.05],
                augment_order=[
                    "random_resized_crop",
                    "random_brightness",
                    "random_contrast",
                    "random_saturation",
                    "random_hue",
                ],
            )}),
        # fmt: on

        # Initialize RLDS Dataset
        self.dataset, self.dataset_length, self.dataset_statistics = self.make_dataset(rlds_config)

    def make_dataset(self, rlds_config):
        return make_interleaved_dataset(**rlds_config)

    def __iter__(self) -> Dict[str, Any]:
        for rlds_batch in self.dataset.as_numpy_iterator():
            yield self.batch_transform(rlds_batch)

    def __len__(self) -> int:
        return self.dataset_length

    # === Explicitly Unused ===
    def __getitem__(self, idx: int) -> None:
        raise NotImplementedError("IterableDataset does not implement map-style __getitem__; see __iter__ instead!")


class EpisodicRLDSDataset(RLDSDataset):
    """Returns full episodes as list of steps instead of individual transitions (useful for visualizations)."""

    def make_dataset(self, rlds_config):
        per_dataset_kwargs = rlds_config["dataset_kwargs_list"]
        assert len(per_dataset_kwargs) == 1, "Only support single-dataset `mixes` for episodic datasets."

        return make_single_dataset(
            per_dataset_kwargs[0],
            train=rlds_config["train"],
            traj_transform_kwargs=rlds_config["traj_transform_kwargs"],
            frame_transform_kwargs=rlds_config["frame_transform_kwargs"],
            load_all_data_for_training=rlds_config["load_all_data_for_training"],
        )

    def __iter__(self) -> Dict[str, Any]:
        for rlds_batch in self.dataset.as_numpy_iterator():
            out = [
                self.batch_transform(tree_map(lambda x: x[i], rlds_batch))  # noqa: B023
                for i in range(rlds_batch["action"].shape[0])
            ]
            yield out


class DummyDataset(Dataset):
    def __init__(
        self,
        action_tokenizer: ActionTokenizer,
        base_tokenizer: PreTrainedTokenizerBase,
        image_transform: ImageTransform,
        prompt_builder_fn: Type[PromptBuilder],
    ) -> None:
        self.action_tokenizer = action_tokenizer
        self.base_tokenizer = base_tokenizer
        self.image_transform = image_transform
        self.prompt_builder_fn = prompt_builder_fn

        # Note =>> We expect the dataset to store statistics for action de-normalization. Specifically, we store the
        # per-dimension 1st and 99th action quantile. The values below correspond to "no normalization" for simplicity.
        self.dataset_statistics = {
            "dummy_dataset": {
                "action": {"q01": np.zeros((7,), dtype=np.float32), "q99": np.ones((7,), dtype=np.float32)}
            }
        }

    def __len__(self):
        # TODO =>> Replace with number of elements in your dataset!
        return 10000

    def __getitem__(self, idx):
        # TODO =>> Load image, action and instruction from disk -- we use dummy values
        image = Image.fromarray(np.asarray(np.random.rand(224, 224, 3) * 255.0, dtype=np.uint8))
        action = np.asarray(np.random.rand(7), dtype=np.float32)
        instruction = "do something spectacular"

        # Add instruction to VLA prompt
        prompt_builder = self.prompt_builder_fn("openvla")
        conversation = [
            {"from": "human", "value": f"What action should the robot take to {instruction}?"},
            {"from": "gpt", "value": self.action_tokenizer(action)},
        ]
        for turn in conversation:
            prompt_builder.add_turn(turn["from"], turn["value"])

        # Tokenize (w/ `base_tokenizer`)
        input_ids = self.base_tokenizer(prompt_builder.get_prompt(), add_special_tokens=True).input_ids
        labels = list(input_ids)

        # Tensorize =>> Run Image Transform to get `pixel_values` =>> Return
        #   =>> IMPORTANT :: IF WE'RE USING HF .forward(..., labels=labels), SHIFTING HAPPENS _INSIDE_ MODEL!
        input_ids, labels = torch.tensor(input_ids), torch.tensor(labels)
        pixel_values = self.image_transform(image)

        # [CRITICAL] We do not want to take the loss for anything but the predicted action tokens!
        labels[: -(len(action) + 1)] = IGNORE_INDEX

        return dict(pixel_values=pixel_values, input_ids=input_ids, labels=labels)
