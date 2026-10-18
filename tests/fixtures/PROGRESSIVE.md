# Синтетические progressive JPEG

Два новых файла progressive-*.jpg.base64 содержат только детерминированный шум,
не фотографии пользователя. Созданы Pillow 12.3.0/libjpeg, progressive=True,
quality=70, random.Random(20261018), последовательно RGB 65×39 и L 17×19.
Для RGB установлен restart_marker_blocks=4, для L — 0. Массив пикселей получен
через rng.randbytes(width * height * channels). Pillow нужен только при
перегенерации этих fixtures; запуск сервиса и тестов использует stdlib.

Дополнительно проверен временный корпус из 48 изображений: L/RGB,
1×1, 7×19, 65×39, 128×192, quality 20/80/95, restart 0/4.

Основание проверки progressive: ITU-T T.81, Annex G (первые и уточняющие
Huffman-проходы, spectral selection, EOB runs и restart intervals):
https://www.w3.org/Graphics/JPEG/itu-t81.pdf
