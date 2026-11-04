"""An inert, bounded subset for SVG image previews; original files stay data."""
from __future__ import annotations

import base64
import hashlib
import xml.etree.ElementTree as ET


MAX_BYTES = 16 * 1024
_NS = "http://www.w3.org/2000/svg"
_TAGS = frozenset({"svg", "g", "path", "rect", "circle", "ellipse", "line", "polyline",
                   "polygon", "text", "tspan", "title", "desc", "defs", "linearGradient",
                   "radialGradient", "stop", "clipPath"})
_ATTRS = frozenset({"id", "x", "y", "x1", "y1", "x2", "y2", "dx", "dy", "d", "cx", "cy",
                    "r", "rx", "ry", "fx", "fy", "width", "height", "viewBox", "preserveAspectRatio",
                    "fill", "fill-opacity", "fill-rule", "stroke", "stroke-width", "stroke-linecap",
                    "stroke-linejoin", "stroke-dasharray", "stroke-dashoffset", "stroke-opacity",
                    "opacity", "transform", "font-family", "font-size", "font-weight", "text-anchor",
                    "dominant-baseline", "offset", "stop-color", "stop-opacity", "gradientUnits",
                    "gradientTransform", "clip-path", "clip-rule"})


class SVGPreviewError(ValueError):
    def __init__(self, code="svg_preview_unavailable"):
        self.code = code
        super().__init__(code)


def svg_preview(content: str) -> dict:
    if not isinstance(content, str):
        raise SVGPreviewError()
    try:
        raw = content.encode("utf-8")
    except UnicodeError:
        raise SVGPreviewError() from None
    lowered = content.lower()
    if (len(raw) > MAX_BYTES or "<!doctype" in lowered or "<!entity" in lowered
            or "<?" in content or "\0" in content):
        raise SVGPreviewError()
    try:
        root = ET.fromstring(content)
    except (ET.ParseError, ValueError):
        raise SVGPreviewError() from None
    if root.tag not in ("svg", "{" + _NS + "}svg"):
        raise SVGPreviewError()
    nodes, pending = [], [(root, 1)]
    while pending:
        node, depth = pending.pop()
        if depth > 64 or len(nodes) >= 2048:
            raise SVGPreviewError()
        nodes.append(node)
        pending.extend((child, depth + 1) for child in node)
    for node in nodes:
        tag = node.tag.removeprefix("{" + _NS + "}")
        if tag not in _TAGS:
            raise SVGPreviewError()
        for name, value in node.attrib.items():
            if name not in _ATTRS or len(value) > MAX_BYTES or "\\" in value:
                raise SVGPreviewError()
            # Only local paint-server/clip references are needed by this subset.
            if "url" in value.lower():
                reference = value.strip()
                if (not reference.startswith("url(#") or not reference.endswith(")")
                        or not reference[5:-1] or not all(char.isalnum() or char in "_-" for char in reference[5:-1])):
                    raise SVGPreviewError()
        node.tag = "{" + _NS + "}" + tag
    # Serialize the parsed subset so processing instructions and comments cannot
    # be smuggled around the element whitelist into the preview document.
    rendered = ET.tostring(root, encoding="utf-8")
    return {"content_type": "image/svg+xml", "content_base64": base64.b64encode(rendered).decode("ascii"),
            "sha256": hashlib.sha256(rendered).hexdigest(),
            "source_sha256": hashlib.sha256(raw).hexdigest()}
