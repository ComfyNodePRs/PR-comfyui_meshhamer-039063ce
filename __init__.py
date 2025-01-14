from custom_nodes.comfyui_controlnet_aux.utils import common_annotator_call, create_node_input_types, MAX_RESOLUTION, \
    run_script
import comfy.model_management as model_management
import numpy as np
import torch
from einops import rearrange
import os, sys
import subprocess, threading
import scipy.ndimage
import cv2
import torch.nn.functional as F
from custom_nodes.comfyui_meshhamer.config import MESH_HAMER_CHECKPOINT

def install_deps():
    try:
        import mediapipe
    except ImportError:
        run_script([sys.executable, '-s', '-m', 'pip', 'install', 'mediapipe'])
        run_script([sys.executable, '-s', '-m', 'pip', 'install', '--upgrade', 'protobuf'])

    try:
        import trimesh
    except ImportError:
        run_script([sys.executable, '-s', '-m', 'pip', 'install', 'trimesh[easy]'])


# Sauce: https://github.com/comfyanonymous/ComfyUI/blob/8c6493578b3dda233e9b9a953feeaf1e6ca434ad/comfy_extras/nodes_mask.py#L309
def expand_mask(mask, expand, tapered_corners):
    c = 0 if tapered_corners else 1
    kernel = np.array([[c, 1, c],
                       [1, 1, 1],
                       [c, 1, c]])
    mask = mask.reshape((-1, mask.shape[-2], mask.shape[-1]))
    out = []
    for m in mask:
        output = m.numpy()
        for _ in range(abs(expand)):
            if expand < 0:
                output = scipy.ndimage.grey_erosion(output, footprint=kernel)
            else:
                output = scipy.ndimage.grey_dilation(output, footprint=kernel)
        output = torch.from_numpy(output)
        out.append(output)
    return torch.stack(out, dim=0)


class Mesh_Hamer_Depth_Map_Preprocessor:
    @classmethod
    def INPUT_TYPES(s):
        types = create_node_input_types(mask_bbox_padding=("INT", {"default": 30, "min": 0, "max": 100}))
        types["optional"].update(
            {
                "mask_type": (["based_on_depth", "tight_bboxes", "original"], {"default": "based_on_depth"}),
                "mask_expand": ("INT", {"default": 5, "min": -MAX_RESOLUTION, "max": MAX_RESOLUTION, "step": 1}),
                "rand_seed": ("INT", {"default": 88, "min": 0, "max": 0xffffffffffffffff}),
                "hand_detect_thr": ("FLOAT", {"default": 0.25, "min": 0, "max": 1, "step": 0.01}),
                "left_hand_thr": ("FLOAT", {"default": 0.6, "min": 0, "max": 1, "step": 0.01}),
                "right_hand_thr": ("FLOAT", {"default": 0.6, "min": 0, "max": 1, "step": 0.01}),
            }
        )
        return types

    RETURN_TYPES = ("IMAGE", "MASK")
    RETURN_NAMES = ("IMAGE", "INPAINTING_MASK")
    FUNCTION = "execute"

    CATEGORY = "ControlNet Preprocessors"

    def execute(self, image, mask_bbox_padding=30, mask_type="based_on_depth", mask_expand=5, resolution=512,
                rand_seed=88, left_hand_thr=0.6, right_hand_thr=0.6,hand_detect_thr=0.25, **kwargs):
        install_deps()
        if not hasattr(self,'model'):
            from custom_nodes.comfyui_meshhamer.mesh_hamer_detector import MeshHamerDetector
            self.model = MeshHamerDetector.from_pretrained(checkpoint=MESH_HAMER_CHECKPOINT, body_detector='vitdet',hand_detect_thr=hand_detect_thr)
        depth_map_list = []
        mask_list = []
        for single_image in image:
            np_image = np.asarray(single_image.cpu() * 255., dtype=np.uint8)
            depth_map, mask, info = self.model(np_image, output_type="np", detect_resolution=resolution,
                                          mask_bbox_padding=mask_bbox_padding, seed=rand_seed,
                                          left_confidence=left_hand_thr, right_confidence=right_hand_thr)
            if mask_type == "based_on_depth":
                H, W = mask.shape[:2]
                mask = cv2.resize(depth_map.copy(), (W, H))
                mask[mask > 0] = 255
            elif mask_type == "tight_bboxes":
                mask = np.zeros_like(mask)
                hand_bboxes = (info or {}).get("bounding_box") or []
                for hand_bbox in hand_bboxes:
                    x_min, x_max, y_min, y_max = hand_bbox
                    mask[y_min:y_max + 1, x_min:x_max + 1, :] = 255  # HWC

            mask = mask[:, :, :1]
            depth_map_list.append(torch.from_numpy(depth_map.astype(np.float32) / 255.0))
            mask_list.append(torch.from_numpy(mask.astype(np.float32) / 255.0))
        depth_maps, masks = torch.stack(depth_map_list, dim=0), rearrange(torch.stack(mask_list, dim=0),
                                                                          "n h w 1 -> n 1 h w")
        return depth_maps, expand_mask(masks, mask_expand, tapered_corners=True)


def normalize_size_base_64(w, h):
    short_side = min(w, h)
    remainder = short_side % 64
    return short_side - remainder + (64 if remainder > 0 else 0)



NODE_CLASS_MAPPINGS = {
    "Meshamer-DepthMapPreprocessor": Mesh_Hamer_Depth_Map_Preprocessor,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "Meshamer-DepthMapPreprocessor": "MeshHamer Hand Refiner",
}