import json
import multiprocessing
import multiprocessing.queues
import multiprocessing.synchronize
import os
import stat
import struct
import subprocess
import tempfile
import platform

from os import path
from typing import Any, Literal, List, Optional, Union

from PIL import Image
from lib.console import ProgressBar
from lib.downloader import FileDownloader
from utils.util import ZipUtils, ToolManager

class BundleExtractor(ToolManager):
    MAIN_EXTRACT_TYPES = [
        "Texture2D", "Sprite", "AudioClip", "Font", "TextAsset",
        "Mesh", "VideoClip", "MonoBehaviour", "Shader",
    ]

    def __init__(self, install_dir: str = "tools", EXTRACT_DIR: str = "output") -> None:
        super().__init__(install_dir)
        self.bin_path = self.ensure_tool()
        self.BUNDLE_EXTRACT_FOLDER = EXTRACT_DIR
        self.install_dir = install_dir

    def _run_uabea(self, args: List[str]) -> subprocess.CompletedProcess:
        """
        Выполняет команду UABEAvalonia CLI.
        Параметры путей к файлам должны передаваться как абсолютные пути (внутри этой функции пути, идущие после -f/-d/-o/-i/-b/-a, преобразуются автоматически).
        cwd (текущая рабочая директория) сохраняется как директория исполняемых файлов UABEA, чтобы гарантировать корректную загрузку плагинов, таких как TexturePlugin.dll.
        """
        # Автоматически преобразуем параметры путей в абсолютные пути

        abs_args: List[str] = ["uabea"]
        path_flags = {"-f", "-d", "-o", "-i", "-b", "-a"}
        i = 0
        while i < len(args):
            abs_args.append(args[i])
            if args[i] in path_flags and i + 1 < len(args):
                i += 1
                abs_args.append(os.path.abspath(args[i]))
            i += 1

        cmd = [self.bin_path] + abs_args
        try:
            return subprocess.run(cmd, capture_output=True, text=True)
        except Exception as e:
            return subprocess.CompletedProcess(cmd, returncode=-1, stdout="", stderr=str(e))

    @staticmethod
    def _parse_list_output(output: str) -> List[dict]:
        """Парсит stdout команды ``UABEAvalonia list`` и возвращает список словарей (dict) с описанием ресурсов."""
        assets: List[dict] = []
        current_source = ""
        is_bundle = False

        for line in output.splitlines():
            s = line.strip()
            if s.startswith("Bundle:"):
                current_source = s.split(":", 1)[1].strip()
                is_bundle = True
                continue
            if s.startswith("Assets File:"):
                current_source = s.split(":", 1)[1].split("(")[0].strip()
                is_bundle = False
                continue
            if not s or s.startswith("-") or s.startswith("PathID") or s.startswith("Total"):
                continue
            cols = s.split()
            try:
                int(cols[0])
            except (ValueError, IndexError):
                continue

            if is_bundle and len(cols) >= 5:
                assets.append({
                    "path_id": cols[0], "entry": cols[1], "type": cols[2],
                    "size": cols[3], "name": " ".join(cols[4:]),
                    "source_path": current_source,
                })
            elif not is_bundle and len(cols) >= 4:
                entry_name = os.path.basename(current_source) if current_source else ""
                assets.append({
                    "path_id": cols[0], "entry": entry_name, "type": cols[1],
                    "size": cols[2], "name": " ".join(cols[3:]),
                    "source_path": current_source,
                })
        return assets

    class _MockReadData:
        """Имитирует (мокает) объект, возвращаемый методом UnityPy obj.read()."""
        def __init__(self, asset_info: dict, raw_bytes: Optional[bytes] = None) -> None:
            self._info = asset_info
            self.m_Name: str = asset_info.get("name", "")
            self._raw = raw_bytes
            if raw_bytes and asset_info.get("type") == "TextAsset":
                self.m_Script = self._parse_textasset_raw(raw_bytes)
            else:
                try:
                    self.m_Script: str = raw_bytes.decode("utf-8") if raw_bytes else ""
                except UnicodeDecodeError:
                    self.m_Script = raw_bytes if raw_bytes else b""
            self.bundleVersion: str = ""

        @staticmethod
        def _parse_textasset_raw(raw: bytes) -> Union[str, bytes]:
            """Извлекает содержимое m_Script из бинарного файла TextAsset, экспортированного через UABEA в raw-формате."""
            import struct
            try:
                if len(raw) < 4:
                    return raw
                name_len = struct.unpack_from("<i", raw, 0)[0]
                if not (0 <= name_len <= 1024) or 4 + name_len > len(raw):
                    return raw
                offset = (4 + name_len + 3) & ~3
                if offset + 4 > len(raw):
                    return raw
                script_len = struct.unpack_from("<i", raw, offset)[0]
                offset += 4
                if not (0 <= script_len <= len(raw) - offset):
                    return raw
                script_bytes = raw[offset:offset + script_len]
                try:
                    return script_bytes.decode("utf-8")
                except UnicodeDecodeError:
                    return script_bytes
            except Exception:
                return raw

    class _MockObj:
        """Имитирует объект UnityPy, обеспечивая совместимость с обращениями к .read(), .source_path, .type.name и др."""
        class _Type:
            def __init__(self, name: str) -> None:
                self.name = name

        def __init__(self, asset_info: dict, read_data: "BundleExtractor._MockReadData") -> None:
            self._info = asset_info
            self._read_data = read_data
            self.source_path: str = asset_info.get("source_path", "")
            self.path_id: str = asset_info.get("path_id", "")
            self.type = BundleExtractor._MockObj._Type(asset_info.get("type", ""))

        def read(self) -> "BundleExtractor._MockReadData":
            return self._read_data

    def search_unity_pack(
        self,
        pack_path: str,
        data_type: Optional[List[str]] = None,
        data_name: Optional[List[str]] = None,
        condition_connect: bool = False,
        read_obj_anyway: bool = False,
        _workers: int = 8,
    ) -> List[Any]:
        """
        Ищет подходящие ресурсы (ассеты) Unity в файле бандла (или директории) и возвращает список объектов _MockObj, совместимых с UnityPy.

        В режиме директории использует list -d для пакетного поиска за один раз; в режиме одиночного файла использует list -f.
        """
        # ── Директория: используем list -d для поиска за один проход, чтобы не запускать дочерний процесс для каждого файла ───────────

        if os.path.isdir(pack_path):
            list_args = ["list", "-d", pack_path, "--recursive"]
            if data_type:
                list_args += ["-t", ",".join(data_type)]
            if data_name and (condition_connect or read_obj_anyway):
                list_args += ["-n", ",".join(f"={n}" for n in data_name)]

            result = self._run_uabea(list_args)
            if result.returncode != 0:
                return []

            candidates = self._parse_list_output(result.stdout)
            data_list: List[Any] = []

            for info in candidates:
                info.setdefault("source_path", pack_path)
                type_passed = (not data_type) or (info["type"] in data_type)
                name_passed = (not data_name) or (info["name"] in data_name)

                if condition_connect or read_obj_anyway:
                    if not (type_passed and name_passed):
                        continue
                else:
                    if not type_passed:
                        continue

                # TextAsset: экспортируем исходное содержимое для заполнения поля m_Script
                raw_bytes: Optional[bytes] = None
                if info["type"] == "TextAsset":
                    source_file = info.get("source_path", "")
                    if source_file:
                        with tempfile.TemporaryDirectory() as tmp_dir:
                            exp = self._run_uabea([
                                "export", "-f", source_file,
                                "-p", info["path_id"],
                                "-o", tmp_dir, "--format", "raw",
                            ])
                            if exp.returncode == 0:
                                for fname in os.listdir(tmp_dir):
                                    fpath = path.join(tmp_dir, fname)
                                    if path.isfile(fpath):
                                        with open(fpath, "rb") as fh:
                                            raw_bytes = fh.read()
                                        break

                read_data = BundleExtractor._MockReadData(info, raw_bytes)
                data_list.append(BundleExtractor._MockObj(info, read_data))

            return data_list

        list_args = ["list", "-f", pack_path]
        if data_type:
            list_args += ["-t", ",".join(data_type)]
        if data_name and (condition_connect or read_obj_anyway):
            list_args += ["-n", ",".join(f"={n}" for n in data_name)]

        result = self._run_uabea(list_args)
        if result.returncode != 0:
            return []

        candidates = self._parse_list_output(result.stdout)
        data_list = []

        for info in candidates:
            info.setdefault("source_path", pack_path)
            type_passed = (not data_type) or (info["type"] in data_type)
            name_passed = (not data_name) or (info["name"] in data_name)

            if condition_connect or read_obj_anyway:
                if not (type_passed and name_passed):
                    continue
            else:
                if not type_passed:
                    continue

            raw_bytes: Optional[bytes] = None
            if info["type"] == "TextAsset":
                with tempfile.TemporaryDirectory() as tmp_dir:
                    exp = self._run_uabea([
                        "export", "-f", pack_path,
                        "-p", info["path_id"],
                        "-o", tmp_dir, "--format", "raw",
                    ])
                    if exp.returncode == 0:
                        for fname in os.listdir(tmp_dir):
                            fpath = path.join(tmp_dir, fname)
                            if path.isfile(fpath):
                                with open(fpath, "rb") as fh:
                                    raw_bytes = fh.read()
                                break

            read_data = BundleExtractor._MockReadData(info, raw_bytes)
            data_list.append(BundleExtractor._MockObj(info, read_data))

        return data_list

    def _patch_crc(self, filepath: str, original_crc_int: int) -> None:
        """
        Добавляет 4-байтовый патч в конец файла, чтобы вернуть общую контрольную сумму CRC файла к значению до внесения изменений.
        Включает подробный отладочный (Debug) вывод процесса проверки и сравнения.
        """

        import io
        from crcmanip.crc import CRC32
        from crcmanip.algorithm import apply_patch, consume

        # [DEBUG] 1. Вычисляем, чему равен текущий CRC сразу после того, как UABEA внесла изменения
        codec_before = CRC32()
        with open(filepath, "rb") as f:
            consume(codec_before, f)
        current_crc = codec_before.digest()

        print(f"\n[DEBUG CRC] Обработка файла: {os.path.basename(filepath)}")
        print(f"  --> Целевой CRC исходного файла (Expected) : 0x{original_crc_int:08X}")
        print(f"  --> CRC после изменения UABEA (Current)    : 0x{current_crc:08X}")


        with open(filepath, "rb") as f:
            data = f.read()

        input_io = io.BytesIO(data)
        output_io = io.BytesIO()
        file_size = len(data)

        codec = CRC32()
        # Вычисляем и применяем патч, добавляя байты в конец текущего файла
        apply_patch(
            crc=codec,
            target_checksum=original_crc_int,
            input_handle=input_io,
            output_handle=output_io,
            target_pos=file_size,
            overwrite=False
        )

        # Записываем пропатченные данные обратно в исходный файл
        patched_data = output_io.getvalue()
        with open(filepath, "wb") as f:
            f.write(patched_data)

        # [DEBUG] 2. Проверяем итоговый CRC после записи патча
        codec_after = CRC32()
        with open(filepath, "rb") as f:
            consume(codec_after, f)
        final_crc = codec_after.digest()

        print(f"  --> CRC после добавления патча (Final)   : 0x{final_crc:08X}")
        if final_crc == original_crc_int:
            print("  --> [УСПЕХ] Проверка CRC после патча совпала!\n")
        else:
            print("  --> [ОШИБКА] Критическая ошибка: CRC после патча не совпадает!\n")

    @staticmethod
    def _new_data_to_file(
        obj_type: str,
        new_data: Any,
        asset_name: str,
        entry: str,
        path_id: str,
        tmp_dir: str,
    ) -> Optional[str]:
        """
        Записывает new_data во временную директорию tmp_dir с именем файла, соответствующим правилам импорта UABEA:
            {AssetName}-{entry}-{PathID}.{ext}
        Возвращает полный путь к записанному файлу; если тип не поддерживается, возвращает None (без отката к UnityPy).
        """

        stem = f"{asset_name}-{entry}-{path_id}" if entry else f"{asset_name}-{path_id}"

        if obj_type == "TextAsset":
            fpath = path.join(tmp_dir, stem + ".txt")
            if isinstance(new_data, str):
                raw = new_data.encode("utf-8", "surrogateescape")
            else:
                raw = bytes(new_data)
            with open(fpath, "wb") as f:
                f.write(raw)
            return fpath

        if obj_type == "Texture2D":
            if isinstance(new_data, Image.Image):
                fpath = path.join(tmp_dir, stem + ".png")
                new_data.save(fpath)
                return fpath
            if isinstance(new_data, (bytes, list)):
                fpath = path.join(tmp_dir, stem + ".dat")
                with open(fpath, "wb") as f:
                    f.write(bytes(new_data) if isinstance(new_data, list) else new_data)
                return fpath

        if obj_type == "Font" and isinstance(new_data, (bytes, list)):
            raw = bytes(new_data) if isinstance(new_data, list) else new_data
            ext = ".otf" if raw[:4] == b"OTTO" else ".ttf"
            fpath = path.join(tmp_dir, stem + ext)
            with open(fpath, "wb") as f:
                f.write(raw)
            return fpath

        if obj_type == "VideoClip" and isinstance(new_data, (bytes, list)):
            fpath = path.join(tmp_dir, stem + ".dat")
            with open(fpath, "wb") as f:
                f.write(bytes(new_data) if isinstance(new_data, list) else new_data)
            return fpath

        if obj_type == "MonoBehaviour" and isinstance(new_data, dict):
            fpath = path.join(tmp_dir, stem + ".json")
            with open(fpath, "wt", encoding="utf-8") as f:
                json.dump(new_data, f, ensure_ascii=False, indent=2)
            return fpath

# AudioClip и другие подобные типы пока не поддерживаются, возвращаем None
        return None

    def modify_and_replace(self, folder_path: str, asset_name: str, new_data: Any) -> None:
        if os.path.isdir(folder_path):
            list_result = self._run_uabea([
                "list", "-d", folder_path, "-n", f"={asset_name}", "--recursive",
            ])
            if list_result.returncode != 0:
                return
            all_matches = [
                m for m in self._parse_list_output(list_result.stdout)
                if m["name"] == asset_name
            ]
            if not all_matches:
                return
            # Дедупликация по файлам: для каждого файла обрабатывается только первое совпадение
            seen_files: set = set()
            for match in all_matches:
                filepath = match.get("source_path", "")
                if filepath and filepath not in seen_files:
                    seen_files.add(filepath)
                    self._import_single_asset(filepath, match, asset_name, new_data)
        else:
            list_result = self._run_uabea([
                "list", "-f", folder_path, "-n", f"={asset_name}",
            ])
            if list_result.returncode != 0:
                return
            matches = [
                m for m in self._parse_list_output(list_result.stdout)
                if m["name"] == asset_name
            ]
            if not matches:
                return
            self._import_single_asset(folder_path, matches[0], asset_name, new_data)

    def _import_single_asset(
        self,
        filepath: str,
        match: dict,
        asset_name: str,
        new_data: Any,
        crc_fix: bool,
    ) -> None:
        """Записывает один ресурс (ассет) в указанный файл бандла и исправляет (патчит) CRC и размер (Size)."""
        try:
            # ── 1. Получаем значение CRC32 исходного файла (в виде целого числа) ──────────────────────
            codec = CRC32()
            with open(filepath, "rb") as f:
                consume(codec, f)
            original_crc_int = codec.digest()

            obj_type = match["type"]
            entry = match["entry"]
            path_id = match["path_id"]

            # ── 2. Записываем new_data во временную директорию ────────────────────────────────
            with tempfile.TemporaryDirectory() as tmp_dir:
                import_file = self._new_data_to_file(
                    obj_type, new_data, asset_name, entry, path_id, tmp_dir
                )
                if import_file is None:
                    # Логика отката здесь удалена, UnityPy больше не используется
                    return

                # ── 3. Обратная запись (импорт) через UABEA ──────────────────────────────────
                imp = self._run_uabea(["import", "-f", filepath, "-i", tmp_dir])
                if imp.returncode != 0:
                    return

            # ── 4. Патчинг CRC (с использованием исходного целочисленного значения CRC) ─────────────────────────
            if crc_fix:
                self._patch_crc(filepath, original_crc_int)

        except Exception:
            pass

    # Сопоставление (маппинг) форматов экспорта UABEA
    _EXPORT_FORMAT: dict = {
        "Texture2D": "png",
        "Sprite":    "png",
        "AudioClip": "wav",
        "TextAsset": "txt",
        "Font":      "raw",
        "VideoClip": "raw",
        "Mesh":      "raw",
        "MonoBehaviour": "json",
        "Shader":    "raw",
    }

    @staticmethod
    def _strip_uabea_suffix(filename: str) -> str:
        """
        Удаляет суффиксы экспорта UABEA: оставляет имя до первой точки или обрезает суффикс -CAB-.
        """

        name_part, _, ext = filename.rpartition(".")

        # 1. В первую очередь разделяем по -CAB- и берем самую левую часть
        if "-CAB-" in name_part:
            return name_part.split("-CAB-")[0] + "." + ext

        # 2. Если CAB отсутствует, пытаемся удалить стандартные суффиксы вида -xxx-xxx
        parts = name_part.rsplit("-", 2)
        if len(parts) >= 2 and len(parts[-1]) > 8: # Простая проверка: PathID обычно довольно длинный
            return parts[0] + "." + ext

        return filename

    def extract_bundle(self, res_path: str, extract_types: Optional[List[str]] = None) -> None:
        """
        Извлекает ресурсы в директорию BUNDLE_EXTRACT_FOLDER/{Type}/ с помощью UABEA CLI export.
        Поддерживает режим одиночного файла (-f) и пакетный режим для директории (-d --recursive).
        """
        types_to_extract = extract_types or self.MAIN_EXTRACT_TYPES
        is_dir = os.path.isdir(res_path)
        path_flag = "-d" if is_dir else "-f"

        for obj_type in types_to_extract:
            fmt = self._EXPORT_FORMAT.get(obj_type, "raw")

            with tempfile.TemporaryDirectory() as tmp_dir:
                export_args = [
                    "export", path_flag, res_path,
                    "-t", obj_type,
                    "-o", tmp_dir,
                    "--format", fmt,
                ]
                if is_dir:
                    export_args.append("--recursive")

                result = self._run_uabea(export_args)

                # Проверяем, действительно ли файлы были экспортированы во временную директорию
                exported_files = [f for f in os.listdir(tmp_dir) if path.isfile(path.join(tmp_dir, f))]
                if result.returncode != 0 or not exported_files:
                    continue

                # Создаем целевую папку только в том случае, если есть ресурсы
                extract_folder = path.join(self.BUNDLE_EXTRACT_FOLDER, obj_type)
                os.makedirs(extract_folder, exist_ok=True)

                for fname in exported_files:
                    src = path.join(tmp_dir, fname)
                    clean_name = self._strip_uabea_suffix(fname)
                    dst = path.join(extract_folder, clean_name)
                    if not path.exists(dst):
                        import shutil
                        shutil.move(src, dst)

    def multiprocess_extract_worker(self, tasks: multiprocessing.Queue, extract_types: Optional[List[str]]) -> None:
        """
        Извлекает пути к бандлам из очереди tasks и поочередно вызывает extract_bundle.
        (Сам по себе UABEA CLI уже достаточно быстр, многопроцессорность используется только для параллельной обработки нескольких файлов.)
        """

        while not tasks.empty():
            try:
                bundle_path = tasks.get_nowait()
                ProgressBar.item_text(path.basename(bundle_path))
                self.extract_bundle(bundle_path, extract_types)
            except Exception:
                pass

    def replace_asset_from_file(self, folder_path: str, asset_name: str, file_path: str, crc_fix: bool = True) -> None:
        if os.path.isdir(folder_path):
            list_result = self._run_uabea([
                "list", "-d", folder_path, "-n", f"={asset_name}", "--recursive",
            ])
            if list_result.returncode != 0:
                return
            all_matches = [
                m for m in self._parse_list_output(list_result.stdout)
                if m["name"] == asset_name
            ]
            if not all_matches:
                return
            seen_files = set()
            for match in all_matches:
                target_filepath = match.get("source_path", "")
                if target_filepath and target_filepath not in seen_files:
                    seen_files.add(target_filepath)
                    self._import_file_direct(target_filepath, match, asset_name, file_path, crc_fix)
        else:
            list_result = self._run_uabea([
                "list", "-f", folder_path, "-n", f"={asset_name}",
            ])
            if list_result.returncode != 0:
                return
            matches = [
                m for m in self._parse_list_output(list_result.stdout)
                if m["name"] == asset_name
            ]
            if not matches:
                return
            self._import_file_direct(folder_path, matches[0], asset_name, file_path, crc_fix)

    def _import_file_direct(self, filepath: str, match: dict, asset_name: str, file_path: str, crc_fix: bool) -> None:
        try:
            # ── 1. Получаем и записываем исходное значение CRC32 до внесения изменений ──────────────────────
            original_crc_int = 0
            if crc_fix:
                from crcmanip.crc import CRC32
                from crcmanip.algorithm import consume
                codec = CRC32()
                with open(filepath, "rb") as f:
                    consume(codec, f)
                original_crc_int = codec.digest()

            obj_type = match["type"]
            entry = match["entry"]
            path_id = match["path_id"]

            ext = os.path.splitext(file_path)[1]
            if not ext:
                if obj_type == "TextAsset": ext = ".txt"
                elif obj_type == "Texture2D": ext = ".png"
                elif obj_type == "Font": ext = ".ttf"
                else: ext = ".dat"

            stem = f"{asset_name}-{entry}-{path_id}" if entry else f"{asset_name}-{path_id}"

            with tempfile.TemporaryDirectory() as tmp_dir:
                import_file = os.path.join(tmp_dir, stem + ext)
                import shutil
                shutil.copy2(file_path, import_file)

                # ── 2. Выполняем импорт изменений ──────────────────────
                imp = self._run_uabea(["import", "-f", filepath, "-i", tmp_dir])
                if imp.returncode != 0:
                    print(f"[DEBUG] UABEA import 失败: {imp.stderr}")
                    return

            # ── 3. Выполняем патчинг CRC и передаем только что вычисленный исходный CRC ────────────────
            if crc_fix:
                self._patch_crc(filepath, original_crc_int)

        except Exception as e:
            print(f"[DEBUG] 发生异常: {e}")
            pass
