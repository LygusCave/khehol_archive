"""crc32_patch.py — патчинг CRC32 на чистом Python + манки-патч для crcmanip.

Обходит баг переполнения PyLong_AsLong в crcmanip_fastcrc.pyd на платформе Windows.

Функции:
  patch_file_crc(path, target_crc)    Добавляет 4 байта в конец файла, чтобы общий CRC32 стал равен target_crc
  monkeypatch_crcmanip()              Заменяет crcmanip.fastcrc.crc_next/crc_prev на реализацию на чистом Python

Алгоритм: патчинг 4-байтового суффикса для CRC32 (zlib, полином 0xEDB88320).
"""

from __future__ import annotations

import zlib

POLY = 0xEDB88320


def _make_forward_table() -> list[int]:
    table = []
    for n in range(256):
        c = n
        for _ in range(8):
            c = (c >> 1) ^ POLY if (c & 1) else (c >> 1)
        table.append(c)
    return table


def _make_high_byte_reverse(forward: list[int]) -> list[int]:
    """Для каждого старшего байта h найти такой индекс idx, что (forward[idx] >> 24) == h.

    Старшие байты прямой таблицы CRC32 (forward) образуют биекцию на диапазоне 0..255, поэтому это отображение корректно определено."""
    rev = [0] * 256
    for idx in range(256):
        h = (forward[idx] >> 24) & 0xFF
        rev[h] = idx
    return rev


FORWARD = _make_forward_table()
REVERSE_HI = _make_high_byte_reverse(FORWARD)


def compute_patch_bytes(data: bytes, target_crc: int) -> bytes:
    """Возвращает 4 байта P так, чтобы zlib.crc32(data + P) == target_crc."""
    current = zlib.crc32(data) & 0xFFFFFFFF
    raw_state = current ^ 0xFFFFFFFF          # внутренний (raw) аккумулятор после обработки data
    target_raw = target_crc ^ 0xFFFFFFFF      # целевой внутренний (raw) аккумулятор

    if raw_state == target_raw:
        return b'\x00\x00\x00\x00'  # уже совпадает; добавление 4 нулевых байтов не влияет на CRC (на самом деле изменит)

    # 4 шага назад: записываем idx для каждого шага и «старшие 24 бита state_prev»
    # прямой ход: R_new = (R_old >> 8) ^ TABLE[(R_old ^ b) & 0xFF]
    #   пусть idx = (R_old ^ b) & 0xFF, тогда TABLE[idx] = R_new ^ (R_old >> 8)
    #   старший байт R_new == старшему байту TABLE[idx] (так как R_old >> 8 — это 24-битное значение)
    #   поэтому idx = REVERSE_HI[R_new >> 24], а старшие 24 бита R_old = младшие 24 бита (R_new ^ TABLE[idx])
    backward = []  # элементы: (idx_for_this_step)
    state = target_raw
    for _ in range(4):
        h = (state >> 24) & 0xFF
        idx = REVERSE_HI[h]
        backward.append(idx)
        # старшие 24 бита R_old:
        high24 = (state ^ FORWARD[idx]) & 0x00FFFFFF
        # младшие 8 бит неизвестны, обнуляем их для следующего шага (не влияет на вывод старшего байта далее)
        state = (high24 << 8) & 0xFFFFFFFF

    # На этом этапе старшие 24 бита state == старшим 24 битам raw_state (гарантируется математически, так как отображение биективно)
    # 4 шага вперёд для вычисления P:
    P = bytearray(4)
    cur = raw_state
    for i in range(4):
        idx = backward[3 - i]
        P[i] = idx ^ (cur & 0xFF)
        cur = (cur >> 8) ^ FORWARD[idx]
        cur &= 0xFFFFFFFF

    if cur != target_raw:
        raise RuntimeError(
            f'Ошибка самопроверки патча CRC32: 0x{cur:08X} != 0x{target_raw:08X}'
        )
    return bytes(P)


def patch_file_crc(file_path: str, target_crc: int) -> bool:
    """Дописывает в конец file_path 4 байта так, чтобы итоговый CRC32 == target_crc.

    Возвращает True, если файл был изменён; False, если CRC уже совпадал и патч не потребовался."""
    with open(file_path, 'rb') as f:
        data = f.read()
    current = zlib.crc32(data) & 0xFFFFFFFF
    if current == (target_crc & 0xFFFFFFFF):
        return False
    patch = compute_patch_bytes(data, target_crc)
    with open(file_path, 'ab') as f:
        f.write(patch)
    # Проверка
    with open(file_path, 'rb') as f:
        new_data = f.read()
    new_crc = zlib.crc32(new_data) & 0xFFFFFFFF
    if new_crc != (target_crc & 0xFFFFFFFF):
        raise RuntimeError(f'После применения патча CRC всё ещё не совпадает: 0x{new_crc:08X} != 0x{target_crc:08X}')
    return True


def monkeypatch_crcmanip() -> None:
    """Заменяет crcmanip.fastcrc.crc_next/crc_prev на реализации на чистом Python.

    Позволяет _patch_crc в bundle.py работать без OverflowError в C-расширении."""
    import sys
    import crcmanip.fastcrc as fastcrc
    import crcmanip.crc as crcmod

    def py_crc_next(codec, source: bytes, value: int) -> int:
        table = codec.lookup_table
        v = value & 0xFFFFFFFF
        for b in source:
            v = (v >> 8) ^ table[(v ^ b) & 0xFF]
        return v & 0xFFFFFFFF

    def py_crc_prev(codec, source: bytes, value: int) -> int:
        # Обратный проход по байтам в обратном порядке (соответствует ветке big_endian=False в CrcPrev из fastcrc.c)
        # прямой ход: R_new = (R_old >> 8) ^ TABLE[(R_old ^ b) & 0xFF]
        # обратный ход: index = R_new >> shift  (shift = 24 для 32-битного режима)
        #               R_old = byte ^ lookup_table_reverse[index] ^ (R_new << 8)
        num_bytes = codec.num_bytes
        num_bits = codec.num_bits
        shift = (num_bytes << 3) - 8
        mask = (1 << num_bits) - 1
        rev_table = codec.lookup_table_reverse
        v = value & mask
        for c in reversed(source):
            index = (v >> shift) & 0xFF
            v = (c ^ rev_table[index] ^ ((v << 8) & mask)) & mask
        return v

    fastcrc.crc_next = py_crc_next
    fastcrc.crc_prev = py_crc_prev
    crcmod.crc_next = py_crc_next
    crcmod.crc_prev = py_crc_prev

if __name__ == '__main__':
    import os, random, tempfile
    random.seed(42)
    for trial in range(5):
        data = bytes(random.randint(0, 255) for _ in range(random.randint(100, 5000)))
        target = random.randint(0, 0xFFFFFFFF)
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(data)
            path = f.name
        try:
            patched = patch_file_crc(path, target)
            with open(path, 'rb') as f:
                new_data = f.read()
            actual = zlib.crc32(new_data) & 0xFFFFFFFF
            ok = actual == target
            print(f'Trial {trial+1}: orig_len={len(data)} target=0x{target:08X} '
                  f'actual=0x{actual:08X} patched={patched} {"✓" if ok else "✗"}')
        finally:
            os.unlink(path)
