"""The reshape has to know which axes are pixels.

It treated every non-batch axis as spatial, so on the live detector's
[1, 3, 512, 512] the channel count — static, and not 512 — was reported as a
spatial dim that could not be reshaped. The warning fired on every start,
naming "[3]", and told the operator their input size was ignored while H and W
were exactly what they asked for.
"""

from pathlib import Path

_SRC = (Path(__file__).resolve().parent.parent
        / "backends/openvino/src/baba_backend_openvino/backend.py")


def _spatial_axes():
    """Load the one pure function without importing openvino."""
    src = _SRC.read_text()
    start = src.index("def spatial_axes(")
    end = src.index("class OpenVINOBackend")
    ns: dict = {}
    exec(compile(src[start:end], str(_SRC), "exec"), ns)
    return ns["spatial_axes"]


def test_nchw_names_the_trailing_two():
    assert _spatial_axes()(4) == {2, 3}


def test_the_channel_axis_is_not_a_pixel_axis():
    """[1, 3, 512, 512]: axis 1 is the channel count. Including it is what
    produced the "[3]" in the warning on every detector start."""
    assert 1 not in _spatial_axes()(4)
    assert 0 not in _spatial_axes()(4)


def test_an_unfamiliar_rank_is_left_alone():
    """Better to leave a shape untouched than to guess which axes are pixels —
    a dynamic axis picked wrongly gets SET to the input size."""
    for rank in (1, 2, 3, 5):
        assert _spatial_axes()(rank) == set()
