"""Разбор готового текста в простые канонические блоки поста."""

import re


_BULLET = re.compile(r"- (.*)")
_NUMBER = re.compile(r"([1-9][0-9]*)\. (.*)")


def parse_text_blocks(text):
    """Пустые строки делят абзацы; явные маркеры образуют списки.

    Длинное тире, Markdown-заголовки и остальной текст остаются буквальными.
    Не меняет Unicode и не угадывает список по оформлению строки.
    """
    if not isinstance(text, str):
        raise ValueError("Текст поста должен быть строкой")

    blocks = []
    paragraph = []
    items = []
    list_kind = None
    expected_number = 1
    after_blank = False

    def finish_paragraph():
        if paragraph:
            blocks.append({"type": "paragraph", "text": "\n".join(paragraph)})
            paragraph.clear()

    def finish_list():
        nonlocal list_kind, expected_number
        if items:
            blocks.append({"type": "list", "items": items.copy(), "ordered": list_kind == "ordered"})
            items.clear()
        list_kind = None
        expected_number = 1

    for original in text.split("\n"):
        line = original.removesuffix("\r")
        if not line.strip():
            finish_paragraph()
            after_blank = True
            continue

        bullet = _BULLET.fullmatch(line)
        number = _NUMBER.fullmatch(line)
        if list_kind == "bullet" and bullet:
            items.append(bullet.group(1))
        elif list_kind == "ordered" and number and int(number.group(1)) == expected_number:
            items.append(number.group(2))
            expected_number += 1
        elif list_kind and not after_blank and original.startswith("  "):
            items[-1] += "\n" + original[2:]
        else:
            finish_list()
            if bullet:
                finish_paragraph()
                list_kind = "bullet"
                items.append(bullet.group(1))
            elif number and int(number.group(1)) == 1:
                finish_paragraph()
                list_kind = "ordered"
                items.append(number.group(2))
                expected_number = 2
            else:
                paragraph.append(original)
        after_blank = False

    finish_list()
    finish_paragraph()
    return blocks or [{"type": "paragraph", "text": text}]
