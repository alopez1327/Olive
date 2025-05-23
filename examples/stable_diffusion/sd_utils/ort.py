# -------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.
# --------------------------------------------------------------------------

import json
import shutil
import sys
from pathlib import Path
from typing import Dict

import onnxruntime as ort
from diffusers import OnnxRuntimeModel, OnnxStableDiffusionPipeline
from onnxruntime import __version__ as OrtVersion
from packaging import version
from sd_utils import config as sd_config

from olive.model import ONNXModelHandler

# ruff: noqa: TID252, T201


def update_dml_config(config_dml: Dict):
    used_passes = {"convert", "optimize"}
    for pass_name in set(config_dml["passes"].keys()):
        if pass_name not in used_passes:
            config_dml["passes"].pop(pass_name, None)
    config_dml["systems"]["local_system"]["accelerators"][0]["execution_providers"] = ["DmlExecutionProvider"]
    return config_dml


def update_cuda_config(config_cuda: Dict):
    if version.parse(OrtVersion) < version.parse("1.17.0"):
        # disable skip_group_norm fusion since there is a shape inference bug which leads to invalid models
        config_cuda["passes"]["optimize_cuda"]["optimization_options"] = {"enable_skip_group_norm": False}
    used_passes = {"convert", "optimize_cuda"}
    for pass_name in set(config_cuda["passes"].keys()):
        if pass_name not in used_passes:
            config_cuda["passes"].pop(pass_name, None)
    config_cuda["systems"]["local_system"]["accelerators"][0]["execution_providers"] = ["CUDAExecutionProvider"]
    return config_cuda


def validate_args(args, provider):
    ort.set_default_logger_severity(4)
    if args.static_dims:
        print(
            "WARNING: the --static_dims option is deprecated, and static shape optimization is enabled by default. "
            "Use --dynamic_dims to disable static shape optimization."
        )

    validate_ort_version(provider)


def validate_ort_version(provider: str):
    if provider == "dml" and version.parse(OrtVersion) < version.parse("1.16.0"):
        print("This script requires onnxruntime-directml 1.16.0 or newer")
        sys.exit(1)
    elif provider == "cuda" and version.parse(OrtVersion) < version.parse("1.17.0"):
        if version.parse(OrtVersion) < version.parse("1.16.2"):
            print("This script requires onnxruntime-gpu 1.16.2 or newer")
            sys.exit(1)
        print(
            f"WARNING: onnxruntime {OrtVersion} has known issues with shape inference for SkipGroupNorm. Will disable"
            " skip_group_norm fusion. onnxruntime-gpu 1.17.0 or newer is strongly recommended!"
        )


def save_optimized_onnx_submodel(submodel_name, provider, model_info):
    footprints_file_path = Path(__file__).resolve().parents[1] / "footprints" / submodel_name / "footprints.json"
    with footprints_file_path.open("r") as footprint_file:
        footprints = json.load(footprint_file)

        conversion_footprint = None
        optimizer_footprint = None
        for footprint in footprints.values():
            from_pass = footprint["from_pass"].lower() if footprint["from_pass"] else ""
            if from_pass == "OnnxConversion".lower():
                conversion_footprint = footprint
                if sd_config.only_conversion:
                    optimizer_footprint = footprint
            elif from_pass == "OrtTransformersOptimization".lower() or from_pass == "OnnxStaticQuantization".lower():
                optimizer_footprint = footprint

        assert conversion_footprint
        assert optimizer_footprint

        unoptimized_olive_model = ONNXModelHandler(**conversion_footprint["model_config"]["config"])
        optimized_olive_model = ONNXModelHandler(**optimizer_footprint["model_config"]["config"])

        model_info[submodel_name] = {
            "unoptimized": {
                "path": Path(unoptimized_olive_model.model_path),
            },
            "optimized": {
                "path": Path(optimized_olive_model.model_path),
            },
        }

        print(f"Unoptimized Model : {model_info[submodel_name]['unoptimized']['path']}")
        print(f"Optimized Model   : {model_info[submodel_name]['optimized']['path']}")


def save_onnx_pipeline(
    has_safety_checker, model_info, optimized_model_dir, unoptimized_model_dir, pipeline, submodel_names
):
    # Save the unoptimized models in a directory structure that the diffusers library can load and run.
    # This is optional, and the optimized models can be used directly in a custom pipeline if desired.
    print("\nCreating ONNX pipeline...")

    if has_safety_checker:
        safety_checker = OnnxRuntimeModel.from_pretrained(model_info["safety_checker"]["unoptimized"]["path"].parent)
    else:
        safety_checker = None

    onnx_pipeline = OnnxStableDiffusionPipeline(
        vae_encoder=OnnxRuntimeModel.from_pretrained(model_info["vae_encoder"]["unoptimized"]["path"].parent),
        vae_decoder=OnnxRuntimeModel.from_pretrained(model_info["vae_decoder"]["unoptimized"]["path"].parent),
        text_encoder=OnnxRuntimeModel.from_pretrained(model_info["text_encoder"]["unoptimized"]["path"].parent),
        tokenizer=pipeline.tokenizer,
        unet=OnnxRuntimeModel.from_pretrained(model_info["unet"]["unoptimized"]["path"].parent),
        scheduler=pipeline.scheduler,
        safety_checker=safety_checker,
        feature_extractor=pipeline.feature_extractor,
        requires_safety_checker=True,
    )

    print("Saving unoptimized models...")
    onnx_pipeline.save_pretrained(unoptimized_model_dir)

    # Create a copy of the unoptimized model directory, then overwrite with optimized models from the olive cache.
    print("Copying optimized models...")
    shutil.copytree(unoptimized_model_dir, optimized_model_dir, ignore=shutil.ignore_patterns("weights.pb"))
    for submodel_name in submodel_names:
        src_path = model_info[submodel_name]["optimized"]["path"]
        dst_path = optimized_model_dir / submodel_name / "model.onnx"
        shutil.copyfile(src_path, dst_path)

    print(f"The optimized pipeline is located here: {optimized_model_dir}")


def get_ort_pipeline(model_dir, common_args, ort_args, guidance_scale):
    ort.set_default_logger_severity(3)

    print("Loading models into ORT session...")
    sess_options = ort.SessionOptions()
    sess_options.enable_mem_pattern = False

    static_dims = not ort_args.dynamic_dims
    batch_size = common_args.batch_size
    image_size = common_args.image_size
    provider = common_args.provider
    vae_sample_size = sd_config.vae_sample_size
    unet_sample_size = sd_config.unet_sample_size

    if static_dims:
        hidden_batch_size = batch_size if (guidance_scale <= 1.0) else batch_size * 2
        # Not necessary, but helps DML EP further optimize runtime performance.
        # batch_size is doubled for sample & hidden state because of classifier free guidance:
        # https://github.com/huggingface/diffusers/blob/46c52f9b9607e6ecb29c782c052aea313e6487b7/src/diffusers/pipelines/stable_diffusion/pipeline_stable_diffusion.py#L672
        sess_options.add_free_dimension_override_by_name("unet_sample_batch", hidden_batch_size)
        sess_options.add_free_dimension_override_by_name("unet_sample_channels", 4)
        sess_options.add_free_dimension_override_by_name("unet_sample_height", image_size // 8)
        sess_options.add_free_dimension_override_by_name("unet_sample_width", image_size // 8)
        sess_options.add_free_dimension_override_by_name("unet_time_batch", 1)
        sess_options.add_free_dimension_override_by_name("unet_hidden_batch", hidden_batch_size)
        sess_options.add_free_dimension_override_by_name("unet_hidden_sequence", 77)

        sess_options.add_free_dimension_override_by_name("decoder_batch", batch_size)
        sess_options.add_free_dimension_override_by_name("decoder_channels", 4)
        sess_options.add_free_dimension_override_by_name("decoder_height", unet_sample_size)
        sess_options.add_free_dimension_override_by_name("decoder_width", unet_sample_size)

        sess_options.add_free_dimension_override_by_name("encoder_batch", batch_size)
        sess_options.add_free_dimension_override_by_name("encoder_channels", 3)
        sess_options.add_free_dimension_override_by_name("encoder_height", vae_sample_size)
        sess_options.add_free_dimension_override_by_name("encoder_width", vae_sample_size)

    provider_map = {
        "dml": "DmlExecutionProvider",
        "cuda": "CUDAExecutionProvider",
    }
    assert provider in provider_map, f"Unsupported provider: {provider}"
    return OnnxStableDiffusionPipeline.from_pretrained(
        model_dir, provider=provider_map[provider], sess_options=sess_options
    )
