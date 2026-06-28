import unittest

from regional_report.commons import strip_preview_emoji
from regional_report.exports import markdown_inline_to_reportlab


class PdfTextSanitizerTests(unittest.TestCase):
    def test_removes_complete_emoji_sequences(self):
        text = "🗓️ Date 🇺🇸 🛢️ Energy ‼️"

        cleaned = strip_preview_emoji(text)

        self.assertEqual(cleaned, " Date   Energy ")
        self.assertNotIn("\ufe0f", cleaned)
        self.assertNotIn("\u200d", cleaned)

    def test_pdf_inline_markup_does_not_leave_dingbat_glyph_inputs(self):
        converted = markdown_inline_to_reportlab(
            "**🛢️ Energy** and [🗓️ calendar](https://example.com) ‼️"
        )

        self.assertEqual(
            converted,
            '<b> Energy</b> and <link href="https://example.com" '
            'color="blue"><u> calendar</u></link> ',
        )
        self.assertNotIn("\ufe0f", converted)

    def test_pdf_inline_markup_preserves_literal_underscores(self):
        converted = markdown_inline_to_reportlab(
            "metric_name and [source_name](https://example.com/a_b)"
        )

        self.assertEqual(
            converted,
            'metric_name and <link href="https://example.com/a_b" '
            'color="blue"><u>source_name</u></link>',
        )


if __name__ == "__main__":
    unittest.main()
