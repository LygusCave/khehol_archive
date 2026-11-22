import os
import sys
import argparse
from pathlib import Path
import re
import winreg
import shutil
from datetime import datetime
from PyTools.MyAwesomeTool.MyUtil import run_cmd, compress_folder_with_progress
from assistance_script.resize_exceldb_to_raw import main as resize_main
from assistance_script.translator import main as translate_main
import hashlib
import os
from xtractor.table import TableProcess
from lib.encryption import calculate_md5
import json
import ssl
import _ssl
from FlatData import *
import FlatData.dump_wrapper
import FlatData.repack_wrapper
from utils.config import Config

KEY_HEX = "" # 05f508236ff3dc4e5dc7cb6fcea0ede0d90a89753dc5e5d36c30393e9b072f48
PRELOAD_PATH = r"BlueArchive_Data\StreamingAssets\PUB\Resource\Preload"
BUNDLE_NAME = 'prologdepengroup-assets-_mx-uis-_mxcommon-_mxprolog-2026-03-13_assets_all_842690403.bundle'
EXCEL_DB_NAME = 'ExcelDB.db'
PROJECT_ROOT = os.path.abspath(".")

BUNDLE_PATH = os.path.join(PRELOAD_PATH, 'windows', BUNDLE_NAME)
DB_PATH = os.path.join(PRELOAD_PATH, 'TableBundles', EXCEL_DB_NAME)
TRANSLATE_PATH = os.path.join(PROJECT_ROOT, PRELOAD_PATH, 'TableJson')


def get_bundled_resource(rel: str) -> str:
    """В режиме onefile PyInstaller распаковывает файлы из --add-data во временную папку sys._MEIPASS; в режиме разработки используется папка со скриптом."""
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


def check_if_file_right_from_install_dir(resource_path: str, md5_json: dict) -> bool:
    pro_path = os.path.join(PROJECT_ROOT, resource_path)
    file_name = os.path.basename(resource_path)
    md5 = calculate_md5(pro_path)
    aim_md5 = ""

    if not os.path.exists(resource_path):
        print(f"Файл ресурсов не существует: {file_name}. Пожалуйста, скопируйте этот exe-файл в папку установки игры и поместите его в одну директорию с BlueArchive.exe")
        return True

    if md5_json.__contains__(resource_path):
        aim_md5 = md5_json[resource_path]

    if md5.__eq__(aim_md5):
        print(f"Файл ресурсов уже пропатчен, пропуск патча: {file_name}")
        return True
    else:
        print(f"Файл ресурсов не пропатчен, сейчас начинается применение патча: {file_name}")
        return False



def get_exe_dir() -> str:
    """Директория, где находится exe (после сборки — папка самого exe, в режиме разработки — папка со скриптом)"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def deploy_winmm_dll():
    """Когда db_password пуст: распаковывает встроенный winmm.dll в папку с exe и выводит инструкцию по автоматической русификации"""
    src = get_bundled_resource('winmm.dll')
    if not os.path.exists(src):
        print("Ошибка: внутри exe не найден ресурс winmm.dll, пожалуйста, пересоберите программу")
        sys.exit(1)
    dst = os.path.join(get_exe_dir(), 'winmm.dll')
    if os.path.abspath(src) != os.path.abspath(dst):
        shutil.copy2(src, dst)
    print("=" * 60)
    print("Ключ базы данных (--db-password) не указан, активирован режим 【Автоматического получения ключа】")
    print()
    print(f"Файл winmm.dll распакован в директорию программы: {dst}")
    print()
    print("Инструкция по применению:")
    print("  1. Убедитесь, что эта программа находится в папке установки игры (в одной директории с BlueArchive.exe)")
    print("  2. Запустите игру обычным способом (через официальный лаунчер или Steam), вводить ключ вручную не нужно")
    print("  3. При запуске игры winmm.dll автоматически перехватит ключ базы данных")
    print("     и запустит эту программу для применения перевода")
    print("  4. Если появится запрос UAC (Контроль учетных записей), нажмите \"Да\"")
    print("  5. Когда в открывшемся окне консоли появится сообщение о завершении патча, можно спокойно играть")
    print()
    print("Подсказка: если после обновления игры перевод слетит, просто запустите эту программу еще раз")
    print("=" * 60)
    input("Нажмите Enter для выхода...")
    sys.exit(0)



def parse_args():
    parser = argparse.ArgumentParser(description="Инструмент установки патча локализации для Blue Archive")
    parser.add_argument(
        '--db-password',
        default=KEY_HEX,
        help="Ключ SQLCipher для ExcelDB.db (в формате hex); если не указан, распакует winmm.dll для автоматического перехвата ключа при запуске игры",
    )
    return parser.parse_args()


if __name__ == '__main__':
    args = parse_args()
    if not args.db_password:
        deploy_winmm_dll()
    json_dic = {}
    Config.db_password = args.db_password
    pro_bundle_path = get_bundled_resource(os.path.join('bundled_assets', BUNDLE_NAME))
    if not os.path.exists(pro_bundle_path):
        # Откат для режима разработки: используется bundle напрямую из папки с игрой (в этом случае исходный и целевой файлы совпадают)
        pro_bundle_path = os.path.join(PROJECT_ROOT, BUNDLE_PATH)
    if os.path.exists("./file_check.json"):
        with open("./file_check.json", 'r', encoding='utf-8') as f:
            json_dic = json.load(f)
    if os.path.exists(pro_bundle_path):
        json_dic[BUNDLE_PATH] = calculate_md5(pro_bundle_path)
    if not check_if_file_right_from_install_dir(DB_PATH, json_dic):
        db_path = os.path.join(PROJECT_ROOT, DB_PATH)
        db_dir_path = os.path.dirname(db_path)
        shutil.copy2(db_path, db_path.replace("ExcelDB.db", "ExcelDB-raw.db"))
        process = TableProcess(str(db_dir_path), str(TRANSLATE_PATH), "FlatData")
        process.process_table("ExcelDB.db", "Extract", True)
        translate_main(TRANSLATE_PATH)
        shutil.rmtree(os.path.join(TRANSLATE_PATH, 'ExcelDB'))
        shutil.copytree(os.path.join(TRANSLATE_PATH, 'translate'), os.path.join(TRANSLATE_PATH, 'ExcelDB'),
                        dirs_exist_ok=True)
        process.process_table("ExcelDB.db", "Repack", True)
        resize_main(db_dir_path)
        with open("./file_check.json", 'w', encoding='utf-8') as f:
            json_dic[DB_PATH] = calculate_md5(db_path)
            json.dump(json_dic, f, ensure_ascii=False, indent=4)
    if not check_if_file_right_from_install_dir(BUNDLE_PATH, json_dic):
        if os.path.abspath(pro_bundle_path) != os.path.abspath(BUNDLE_PATH):
            shutil.copy2(pro_bundle_path, BUNDLE_PATH)
    if os.path.exists(TRANSLATE_PATH):
        shutil.rmtree(TRANSLATE_PATH)
    # input("Выполнение программы завершено, нажмите любую клавишу для выхода:\n")

