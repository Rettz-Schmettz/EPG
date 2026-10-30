#!/usr/bin/env python3
"""Извлекает из M3U-плейлиста только группы и названия каналов (без ссылок и токена).

Использование:
    python extract_channels.py playlist.m3u channels.tsv
"""
import re
import sys

ATTR_RE = re.compile(r'([\w-]+)="([^"]*)"')


def parse(path):
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        for line in f:
            line = line.rstrip("\r\n")
            if not line.startswith("#EXTINF"):
                continue
            # название — всё после первой запятой, идущей после последнего атрибута
            last_quote = line.rfind('"')
            comma = line.find(",", last_quote + 1 if last_quote != -1 else 0)
            if comma == -1:
                continue
            name = line[comma + 1:].strip()
            attrs = dict(ATTR_RE.findall(line[:comma]))
            yield attrs.get("group-title", ""), name


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    src, dst = sys.argv[1], sys.argv[2]
    seen = set()
    rows = []
    for group, name in parse(src):
        if not name or name in seen:
            continue
        seen.add(name)
        rows.append((group, name))
    with open(dst, "w", encoding="utf-8", newline="\n") as f:
        f.write("# group\tname — сгенерировано extract_channels.py, ссылок здесь нет\n")
        for group, name in rows:
            f.write(f"{group}\t{name}\n")
    print(f"{len(rows)} каналов → {dst}")


if __name__ == "__main__":
    main()
