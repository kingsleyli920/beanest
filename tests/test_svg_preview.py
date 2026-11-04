import base64
import unittest
import xml.etree.ElementTree as ET

from local_agent_coordinator.svg_preview import SVGPreviewError, svg_preview


class SVGPreviewTests(unittest.TestCase):
    def test_static_shapes_and_local_gradient_are_visible(self):
        source = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 80"><defs><linearGradient id="bean"><stop offset="0" stop-color="#ccd"/></linearGradient></defs><rect width="120" height="80" fill="url(#bean)"/><text x="10" y="40">豆窝</text></svg>'
        result = svg_preview(source)
        root = ET.fromstring(base64.b64decode(result["content_base64"], validate=True))
        self.assertEqual(root.tag, '{http://www.w3.org/2000/svg}svg')
        self.assertEqual(root.find('{http://www.w3.org/2000/svg}text').text, '豆窝')
        self.assertEqual(result['content_type'], 'image/svg+xml')

    def test_active_external_and_foreign_content_is_not_previewed(self):
        parts = ['<script>alert(1)</script>', '<foreignObject/>', '<image href="https://example.test/pixel"/>',
                 '<use href="#self"/>', '<rect onload="alert(1)"/>', '<rect style="fill:red"/>',
                 '<rect fill="url(https://example.test/x)"/>', '<a href="https://example.test"/>',
                 '<animate attributeName="x"/>', '<x:rect xmlns:x="https://example.test"/>',
                 '<rect fill="u\\72l(https://example.test/x)"/>']
        for part in parts:
            with self.subTest(part=part), self.assertRaises(SVGPreviewError):
                svg_preview('<svg>' + part + '</svg>')

    def test_entities_instructions_invalid_roots_and_limits_are_refused(self):
        for source in ['<!DOCTYPE svg [<!ENTITY x "entity">]><svg>&x;</svg>',
                       '<?xml-stylesheet href="https://example.test"?><svg/>',
                       '<html/>', '<svg>', '<svg>' + 'x' * 16384 + '</svg>']:
            with self.subTest(source=source[:60]), self.assertRaises(SVGPreviewError):
                svg_preview(source)

    def test_deep_tree_is_refused_before_recursive_serialization(self):
        with self.assertRaises(SVGPreviewError):
            svg_preview('<svg>' + '<g>' * 1100 + '</g>' * 1100 + '</svg>')
