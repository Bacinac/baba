#!/usr/bin/env python3
"""Bake uint8 → float32 normalisation + HWC→CHW permute into a DETR ONNX.

Use case: a HuggingFace-style RT-DETR / D-FINE ONNX export expects input
`pixel_values [B, 3, H, W] float32` already normalised to [0, 1]. The
detector then has to do `astype(float32) / 255.0` + `transpose(0, 3, 1, 2)`
+ `ascontiguousarray` on every batch — measured at ~20 ms per 8-image
batch on a 6-core box, all of it CPU work the GPU could trivially absorb.

This script rewrites the graph so the model accepts `image [B, H, W, 3]
uint8` directly. Three new ops are prepended:

    image (uint8 NHWC)
      → Cast(to=FLOAT)             # uint8 → float32
      → Mul(× scale)               # → [0, 1], or ImageNet-scaled for --family rfdetr
      → Sub(bias)                  # rfdetr only: ImageNet mean/std shift
      → Transpose(perm=[0,3,1,2])  # NHWC → NCHW
      → existing graph (was pixel_values consumers)

`--family` MUST match BABA_DETECTOR_MODEL_FAMILY: D-FINE / RT-DETR exports want
plain [0, 1], RF-DETR / LWDETR want ImageNet normalisation, and a model fed the
wrong convention returns confident nonsense rather than failing.

CPU resize+letterbox stays for now because dynamic-spatial Resize requires
Shape/Slice/Concat plumbing that the TRT optimisation profile doesn't love
without dynamic_shapes support in the backend. That's the next iteration.

Usage:
    python tools/bake_preproc.py \\
        --input  /models/rtdetrv2-r18.onnx \\
        --output /models/rtdetrv2-r18.preproc.onnx \\
        [--input-size 640]

Detector autodetects the baked variant via the model's declared input dtype
(see `backend.inputs[0].dtype`). To opt in, just point BABA_DETECTOR_MODEL
at the .preproc.onnx and recreate baba-detector.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import numpy as np
import onnx
from onnx import TensorProto, helper, numpy_helper

log = logging.getLogger("bake_preproc")


# RF-DETR / LWDETR exports expect ImageNet-normalised input, D-FINE / RT-DETR
# plain [0, 1]. Folding mean/std into the scale keeps both at one Mul (+ one Sub),
# so the ImageNet family costs a single extra op rather than a separate chain:
#   (x/255 - mean) / std  ==  x * 1/(255*std) - mean/std
_IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
_IMAGENET_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)


def _norm_constants(family: str) -> tuple[np.ndarray, np.ndarray | None]:
    """Per-channel scale and bias applied while the tensor is still NHWC.

    Returns (scale, bias); bias is None for the plain [0, 1] convention. Both are
    shape [3] so they broadcast over the trailing channel axis.
    """
    if family == "rfdetr":
        return (
            (1.0 / (255.0 * _IMAGENET_STD)).astype(np.float32),
            (_IMAGENET_MEAN / _IMAGENET_STD).astype(np.float32),
        )
    return np.array(1.0 / 255.0, dtype=np.float32), None


def _first_input(model: onnx.ModelProto) -> onnx.ValueInfoProto:
    if not model.graph.input:
        raise ValueError("model has no inputs")
    # External weights show up under graph.input too — filter to the genuine
    # tensors (those without a matching initialiser).
    initialiser_names = {init.name for init in model.graph.initializer}
    candidates = [i for i in model.graph.input if i.name not in initialiser_names]
    if not candidates:
        raise ValueError("model has no non-initialiser inputs")
    if len(candidates) > 1:
        names = [c.name for c in candidates]
        raise ValueError(f"model has multiple inputs {names}; not supported")
    return candidates[0]


def _input_dtype(value_info: onnx.ValueInfoProto) -> int:
    return value_info.type.tensor_type.elem_type


def _build_rgb_preproc(
    new_input_name: str, orig_name: str, batch_symbol: str, input_size: int,
    family: str = "dfine",
) -> tuple[onnx.ValueInfoProto, list[onnx.NodeProto], list[onnx.TensorProto]]:
    """Cast + normalise + Transpose NHWC→NCHW for uint8 RGB input.

    Input: image (B, input_size, input_size, 3) uint8
    Output: orig_name (B, 3, input_size, input_size) float32, normalised to the
    convention `family` expects.
    """
    new_input = helper.make_tensor_value_info(
        new_input_name,
        TensorProto.UINT8,
        [batch_symbol, input_size, input_size, 3],
    )
    cast_out = f"{new_input_name}__cast"
    cast_node = helper.make_node(
        "Cast", inputs=[new_input_name], outputs=[cast_out],
        to=TensorProto.FLOAT, name="preproc_cast",
    )
    scale_arr, bias_arr = _norm_constants(family)
    scale = numpy_helper.from_array(scale_arr, name="preproc_scale")
    inits = [scale]
    mul_out = f"{new_input_name}__norm"
    nodes = [cast_node, helper.make_node(
        "Mul", inputs=[cast_out, "preproc_scale"], outputs=[mul_out],
        name="preproc_mul",
    )]
    if bias_arr is not None:
        bias = numpy_helper.from_array(bias_arr, name="preproc_bias")
        inits.append(bias)
        shifted = f"{new_input_name}__shift"
        nodes.append(helper.make_node(
            "Sub", inputs=[mul_out, "preproc_bias"], outputs=[shifted],
            name="preproc_sub",
        ))
        mul_out = shifted
    nodes.append(helper.make_node(
        "Transpose", inputs=[mul_out], outputs=[orig_name],
        perm=[0, 3, 1, 2], name="preproc_transpose",
    ))
    return new_input, nodes, inits


# BT.709 limited-range YUV→RGB conversion. Y is biased by 16 to give it the
# 16–235 head-and-foot-room a broadcast-style YUV signal carries; UV are
# centred on 128. Per-pixel coefficients (after subtracting the bias) come
# straight from ITU-R BT.709 §3 table A.1.
#
# The MatMul is written so its right operand layout is [input_channel × output_channel]:
# we multiply a (..., 3) YUV row-vector by this 3×3 matrix to get a (..., 3)
# RGB row-vector. Row 0 is the per-output coefficient on Y (1.164 to every
# channel), row 1 is U's contribution (0 to R, -0.213 to G, 2.112 to B),
# row 2 is V's contribution.
#
# Output is in the conventional 0–255 RGB range *before* the /255 step that
# follows; out-of-gamut Y/UV combinations may overshoot which we don't clip
# (Conv/BatchNorm in the model swallows the noise; clipping would cost an
# extra Min/Max pair for negligible accuracy).
_BT709_LIMITED_BIAS = np.array([16.0, 128.0, 128.0], dtype=np.float32)
_BT709_LIMITED_MATRIX = np.array([
    # Y_out   U_out   V_out   (input)
    [1.164,   1.164,  1.164],   # → R   G   B   (Y contributes)
    [0.000,  -0.213,  2.112],   # → R   G   B   (U contributes)
    [1.793,  -0.533,  0.000],   # → R   G   B   (V contributes)
], dtype=np.float32)


def _build_nv12_preproc(
    new_input_name: str, orig_name: str, batch_symbol: str, input_size: int,
    family: str = "dfine",
) -> tuple[onnx.ValueInfoProto, list[onnx.NodeProto], list[onnx.TensorProto]]:
    """Slice Y/UV, 2× upsample UV (nearest), BT.709 limited YUV→RGB matrix,
    normalise, transpose to NCHW. For uint8 NV12 input.

    Input layout: (B, H + H/2, W) uint8 — Y plane first, interleaved UV below.
    Output: (B, 3, H, W) float32 in roughly [0, 1].
    H = W = input_size; both must be even.
    """
    if input_size % 2:
        raise ValueError(f"NV12 input_size must be even; got {input_size}")
    H = input_size
    W = input_size
    H2 = H // 2
    W2 = W // 2

    new_input = helper.make_tensor_value_info(
        new_input_name, TensorProto.UINT8,
        [batch_symbol, H + H2, W],
    )

    nodes: list[onnx.NodeProto] = []
    inits: list[onnx.TensorProto] = []

    # 1. Cast uint8 → float32 once for the whole NV12 buffer.
    cast_out = "nv12__f32"
    nodes.append(helper.make_node(
        "Cast", [new_input_name], [cast_out],
        to=TensorProto.FLOAT, name="nv12_cast",
    ))

    # 2. Slice Y plane out of the top of the buffer.
    # Slice(data, starts, ends, axes, steps). axes=[1] (the row dimension).
    y_starts = numpy_helper.from_array(np.array([0], dtype=np.int64), name="nv12_y_starts")
    y_ends = numpy_helper.from_array(np.array([H], dtype=np.int64), name="nv12_y_ends")
    axes_row = numpy_helper.from_array(np.array([1], dtype=np.int64), name="nv12_axes_row")
    steps_one = numpy_helper.from_array(np.array([1], dtype=np.int64), name="nv12_steps_one")
    inits.extend([y_starts, y_ends, axes_row, steps_one])
    y_plane = "nv12__y"  # (B, H, W) float32
    nodes.append(helper.make_node(
        "Slice",
        [cast_out, "nv12_y_starts", "nv12_y_ends", "nv12_axes_row", "nv12_steps_one"],
        [y_plane], name="nv12_slice_y",
    ))

    # 3. Slice UV interleaved plane out of the bottom of the buffer.
    uv_starts = numpy_helper.from_array(np.array([H], dtype=np.int64), name="nv12_uv_starts")
    uv_ends = numpy_helper.from_array(np.array([H + H2], dtype=np.int64), name="nv12_uv_ends")
    inits.extend([uv_starts, uv_ends])
    uv_flat = "nv12__uv_flat"  # (B, H/2, W) float32 — UVUV...
    nodes.append(helper.make_node(
        "Slice",
        [cast_out, "nv12_uv_starts", "nv12_uv_ends", "nv12_axes_row", "nv12_steps_one"],
        [uv_flat], name="nv12_slice_uv",
    ))

    # 4. Reshape (B, H/2, W) → (B, H/2, W/2, 2) so U and V land on a real
    #    channel axis (last dim).
    uv_split_shape = numpy_helper.from_array(
        np.array([-1, H2, W2, 2], dtype=np.int64), name="nv12_uv_split_shape",
    )
    inits.append(uv_split_shape)
    uv_pairs = "nv12__uv_pairs"
    nodes.append(helper.make_node(
        "Reshape", [uv_flat, "nv12_uv_split_shape"], [uv_pairs],
        name="nv12_uv_reshape",
    ))

    # 5. Nearest-neighbour upsample 2× on H and W of the UV pairs.
    #    Result: (B, H, W, 2). Nearest is enough for detection — chroma
    #    detail isn't visually critical, and bilinear would cost another
    #    op + read-modify-write for very little accuracy delta.
    uv_scales = numpy_helper.from_array(
        np.array([1.0, 2.0, 2.0, 1.0], dtype=np.float32), name="nv12_uv_scales",
    )
    inits.append(uv_scales)
    uv_full = "nv12__uv_full"  # (B, H, W, 2) float32
    nodes.append(helper.make_node(
        "Resize",
        [uv_pairs, "", "nv12_uv_scales"],
        [uv_full],
        mode="nearest",
        coordinate_transformation_mode="asymmetric",
        nearest_mode="floor",
        name="nv12_uv_upsample",
    ))

    # 6. Reshape Y from (B, H, W) → (B, H, W, 1) so it can concat with UV
    #    on the last axis.
    y_expand_shape = numpy_helper.from_array(
        np.array([-1, H, W, 1], dtype=np.int64), name="nv12_y_expand_shape",
    )
    inits.append(y_expand_shape)
    y_4d = "nv12__y_4d"
    nodes.append(helper.make_node(
        "Reshape", [y_plane, "nv12_y_expand_shape"], [y_4d],
        name="nv12_y_reshape",
    ))

    # 7. Concat Y + UV → (B, H, W, 3) YUV NHWC.
    yuv = "nv12__yuv"
    nodes.append(helper.make_node(
        "Concat", [y_4d, uv_full], [yuv],
        axis=-1, name="nv12_concat_yuv",
    ))

    # 8. Subtract BT.709 limited bias (16, 128, 128).
    yuv_bias = numpy_helper.from_array(_BT709_LIMITED_BIAS, name="nv12_bt709_bias")
    inits.append(yuv_bias)
    yuv_centered = "nv12__yuv_centered"
    nodes.append(helper.make_node(
        "Sub", [yuv, "nv12_bt709_bias"], [yuv_centered],
        name="nv12_sub_bias",
    ))

    # 9. MatMul to apply the 3×3 BT.709 limited matrix → RGB in [0, 255].
    bt709_init = numpy_helper.from_array(
        _BT709_LIMITED_MATRIX, name="nv12_bt709_matrix",
    )
    inits.append(bt709_init)
    rgb_255 = "nv12__rgb255"
    nodes.append(helper.make_node(
        "MatMul", [yuv_centered, "nv12_bt709_matrix"], [rgb_255],
        name="nv12_matmul_yuv2rgb",
    ))

    # 10. Normalise to whatever convention the detector family expects.
    scale_arr, bias_arr = _norm_constants(family)
    inits.append(numpy_helper.from_array(scale_arr, name="nv12_scale"))
    rgb_norm = "nv12__rgb_norm"
    nodes.append(helper.make_node(
        "Mul", [rgb_255, "nv12_scale"], [rgb_norm],
        name="nv12_normalise",
    ))
    if bias_arr is not None:
        inits.append(numpy_helper.from_array(bias_arr, name="nv12_bias"))
        shifted = "nv12__rgb_shift"
        nodes.append(helper.make_node(
            "Sub", [rgb_norm, "nv12_bias"], [shifted], name="nv12_imagenet_shift",
        ))
        rgb_norm = shifted

    # 11. Transpose NHWC → NCHW so the downstream graph sees the original
    #     pixel_values shape unchanged.
    nodes.append(helper.make_node(
        "Transpose", [rgb_norm], [orig_name],
        perm=[0, 3, 1, 2], name="nv12_transpose",
    ))

    return new_input, nodes, inits


def bake(
    src: Path,
    dst: Path,
    input_size: int,
    new_input_name: str = "image",
    input_format: str = "rgb",
    family: str = "dfine",
) -> None:
    """Insert preprocessing nodes ahead of the existing graph's first op."""
    log.info("Loading %s", src)
    model = onnx.load(str(src))

    original_input = _first_input(model)
    if _input_dtype(original_input) != TensorProto.FLOAT:
        raise ValueError(
            f"expected float32 input; got dtype id {_input_dtype(original_input)}. "
            f"Model already baked?"
        )

    orig_name = original_input.name
    log.info(
        "Original input: %s dtype=float32 shape=%s",
        orig_name,
        [d.dim_value or d.dim_param or "?" for d in original_input.type.tensor_type.shape.dim],
    )

    batch_dim = original_input.type.tensor_type.shape.dim[0]
    batch_symbol = batch_dim.dim_param or "batch_size"

    if input_format == "rgb":
        new_input, new_nodes, new_initialisers = _build_rgb_preproc(
            new_input_name, orig_name, batch_symbol, input_size, family,
        )
    elif input_format == "nv12":
        new_input, new_nodes, new_initialisers = _build_nv12_preproc(
            new_input_name, orig_name, batch_symbol, input_size, family,
        )
    else:
        raise ValueError(
            f"unsupported input_format: {input_format!r} (expected 'rgb' or 'nv12')"
        )
    log.info("Baking %s preprocessing chain (%d nodes, %d initialisers)",
             input_format, len(new_nodes), len(new_initialisers))

    # Replace the input in graph.input. Existing initialisers stay where
    # they are; we only swap the user-facing input.
    new_graph_inputs = [new_input]
    for inp in model.graph.input:
        if inp.name == orig_name:
            continue
        new_graph_inputs.append(inp)

    del model.graph.input[:]
    model.graph.input.extend(new_graph_inputs)
    model.graph.initializer.extend(new_initialisers)
    # Insert new nodes at the start so they execute first. The list order
    # in graph.node IS the execution order, so prepend explicitly.
    existing_nodes = list(model.graph.node)
    del model.graph.node[:]
    model.graph.node.extend(new_nodes)
    model.graph.node.extend(existing_nodes)

    # Bump opset to a version that supports all ops we inserted. Cast
    # since opset 1; Mul since opset 1; Transpose since opset 1. The
    # source opset (16) is already plenty.
    log.info("Running shape inference + checker")
    try:
        model = onnx.shape_inference.infer_shapes(model)
    except onnx.shape_inference.InferenceError as e:
        # shape_inference can fail on graphs with rank-mismatching ops,
        # but the runtime check below is the real gate. Don't abort yet.
        log.warning("shape_inference failed (non-fatal): %s", e)

    onnx.checker.check_model(model)

    log.info("Saving %s", dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    onnx.save(model, str(dst))

    # Print a tidy summary so the operator can sanity-check before
    # building TRT engines.
    log.info("=== resulting input/output ===")
    for inp in model.graph.input:
        shape = [d.dim_value or d.dim_param or "?" for d in inp.type.tensor_type.shape.dim]
        dtype_name = TensorProto.DataType.Name(inp.type.tensor_type.elem_type)
        log.info("  in  %s: %s shape=%s", inp.name, dtype_name, shape)
    for out in model.graph.output:
        shape = [d.dim_value or d.dim_param or "?" for d in out.type.tensor_type.shape.dim]
        dtype_name = TensorProto.DataType.Name(out.type.tensor_type.elem_type)
        log.info("  out %s: %s shape=%s", out.name, dtype_name, shape)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--input", "-i", type=Path, required=True,
                        help="Source ONNX (float32 NCHW input)")
    parser.add_argument("--output", "-o", type=Path, required=True,
                        help="Destination ONNX (uint8 NHWC input)")
    parser.add_argument("--input-size", type=int, default=640,
                        help="Spatial size the upstream pipeline resizes to (default: 640)")
    parser.add_argument("--input-name", default="image",
                        help="Name of the new uint8 input tensor")
    parser.add_argument(
        "--input-format", choices=("rgb", "nv12"), default="rgb",
        help=(
            "Pixel format the new model input accepts. "
            "'rgb' bakes only Cast+Mul+Transpose (caller still does NV12→RGB upstream); "
            "'nv12' additionally bakes a BT.709 limited-range YUV→RGB chain so the "
            "decoder can hand NV12 straight to the model and skip libswscale entirely."
        ),
    )
    parser.add_argument(
        "--family", choices=("dfine", "rfdetr"), default="dfine",
        help=(
            "Input normalisation convention. 'dfine' (D-FINE / RT-DETR) scales to "
            "[0, 1]; 'rfdetr' (RF-DETR / LWDETR) additionally applies the ImageNet "
            "mean/std the export was trained with. Must match "
            "BABA_DETECTOR_MODEL_FAMILY or the boxes are garbage."
        ),
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    if not args.input.exists():
        log.error("Input not found: %s", args.input)
        return 1
    if args.output.exists():
        log.warning("Overwriting existing %s", args.output)

    bake(
        args.input, args.output, args.input_size, args.input_name,
        input_format=args.input_format, family=args.family,
    )
    log.info("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
