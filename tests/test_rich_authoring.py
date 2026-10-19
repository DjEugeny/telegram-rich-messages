import unittest

from core import compile_post, validate_publication_structure


class RichAuthoringTests(unittest.TestCase):
    def test_hidden_or_empty_heading_does_not_satisfy_publication_policy(self):
        for blocks in (
            [{'type': 'details', 'summary': 'Ещё', 'blocks': [{'type': 'heading', 'text': 'Спрятан'}]}],
            [{'type': 'heading', 'text': ''}],
            [{'type': 'heading', 'text': '** **'}],
            [{'type': 'heading', 'text': [{'type': 'bold', 'text': '\u00a0'}]}],
        ):
            with self.subTest(blocks=blocks):
                post = {'blocks': blocks}
                compile_post(post)  # Сохранение исходного черновика остаётся допустимым.
                with self.assertRaises(ValueError):
                    validate_publication_structure(post)

    def test_inline_summary_matches_literal_and_formatted_text(self):
        post = {'blocks': [
            {'type': 'heading', 'level': 3, 'text': ['Тема ', {'type': 'bold', 'text': '<А>'}]},
            {'type': 'paragraph', 'text': [
                '**Важно** ', {'type': 'italic', 'text': 'тон'},
                {'type': 'underline', 'text': 'линия'},
                {'type': 'strikethrough', 'text': 'старое'},
                {'type': 'code', 'text': '<strong>буквально</strong>'},
                {'type': 'text', 'text': '**буквально** <h1>не заголовок</h1>'},
                {'type': 'custom_emoji', 'text': '⭐', 'custom_emoji_id': '123456'},
            ]},
        ]}
        summary = validate_publication_structure(post)
        self.assertEqual(summary['headings'], [{'level': 3, 'text': 'Тема <А>'}])
        self.assertEqual(summary['inline'], {'bold': 2, 'italic': 1, 'underline': 1,
                                            'strikethrough': 1, 'code': 1, 'custom_emoji': 1})
        self.assertEqual(summary['blocks'], {'heading': 1, 'paragraph': 1})


if __name__ == '__main__':
    unittest.main()
