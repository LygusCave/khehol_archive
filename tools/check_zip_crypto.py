import pyzipper
import os

PROJECT_ROOT = os.path.dirname(os.path.abspath("."))
zip_path = os.path.join(PROJECT_ROOT, 'BlueArchive_Data/StreamingAssets/PUB/Resource/Preload/TableBundles/Excel.zip')
with pyzipper.AESZipFile(zip_path, 'r') as zf:
    # Проверить тип шифрования первого зашифрованного файла
    # Получить информацию обо всех файлах
    infos = zf.infolist()
    if not infos:
        print("Архив пуст")
    else:
        first_info = infos[0]  # Взять первый файл
        compression = first_info.compress_type
        encryption = getattr(first_info, 'encryption', None)  # Специфичный атрибут pyzipper
        print(f"Алгоритм сжатия: {compression}, тип шифрования: {encryption}")
        is_encrypted = (first_info.flag_bits & 0x1) != 0
        print(f"Зашифрован ли: {is_encrypted}")
