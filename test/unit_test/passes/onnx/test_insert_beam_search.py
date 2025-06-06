# -------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.
# --------------------------------------------------------------------------

from transformers import AutoConfig

from olive.model import CompositeModelHandler
from olive.passes.olive_pass import create_pass_from_dict
from olive.passes.onnx.insert_beam_search import InsertBeamSearch
from test.unit_test.utils import get_onnx_model


def test_insert_beam_search_pass(tmp_path):
    # setup
    input_models = []
    input_models.append(get_onnx_model())
    input_models.append(get_onnx_model())
    composite_model = CompositeModelHandler(
        input_models,
        ["encoder_decoder_init", "decoder"],
        model_attributes=AutoConfig.from_pretrained("openai/whisper-base.en").to_dict(),
    )

    p = create_pass_from_dict(InsertBeamSearch, {}, disable_search=True)
    output_folder = str(tmp_path / "onnx")

    # execute
    p.run(composite_model, output_folder)
