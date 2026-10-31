#!/usr/bin/env python3
"""Собирает один XMLTV-файл телепрограммы под конкретный плейлист.

Идея: в итоговом файле id и display-name каждого канала равны названию канала
из плейлиста. UniPlayer (и другие плееры) сопоставляют канал с программой по
названию на экране, поэтому сам плейлист провайдера менять не нужно.

Источники берутся по приоритету из config.json: канал получает программу из
первого источника, где она для него нашлась. Каналы вида «Первый канал (+4)»
получают программу базового канала, сдвинутую на N часов назад.

Использование:
    python build_epg.py [--config config.json] [--out public]
"""
import argparse
import datetime as dt
import gzip
import json
import lzma
import os
import re
import shutil
import sys
import tempfile
import time
import unicodedata
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from xml.sax.saxutils import escape, quoteattr

HERE = os.path.dirname(os.path.abspath(__file__))
UA = "Mozilla/5.0 (epg-builder; personal use)"

SHIFT_RE = re.compile(r"\s*\(\s*\+\s*(\d{1,2})\s*\)\s*")
# «качество» и служебные пометки, которые не влияют на программу
QUALITY = {"hd", "fhd", "uhd", "sd", "4k", "8k", "hdr", "h264", "h265", "hevc",
           "50fps", "60fps", "orig", "original", "hq", "lq"}
NOISE_RE = re.compile(r"\((?:only on matchday|резерв|backup|test)\)", re.I)


# ---------------------------------------------------------------- нормализация

def norm(name: str) -> str:
    """Строгий ключ: регистр, ё, пунктуация, лишние пробелы."""
    s = unicodedata.normalize("NFKC", name).lower().replace("ё", "е")
    s = NOISE_RE.sub(" ", s)
    s = s.replace("&", " and ").replace("+", " plus ")
    s = re.sub(r"[\[\]().,:;!?'\"«»|/\\_*-]+", " ", s)
    return " ".join(s.split())


def loose(name: str) -> str:
    """Мягкий ключ: дополнительно без пометок качества (HD, 4K, HDR…)."""
    return " ".join(t for t in norm(name).split() if t not in QUALITY)


_HOMO = str.maketrans("aceopxykmtbh", "асеорхукмтвн")


def compact(name: str) -> str:
    """Самый мягкий ключ: как loose, но без пробелов и с латиницей-двойниками → кириллица."""
    return loose(name).replace(" ", "").translate(_HOMO)


COUNTRY = {"IS": "IL", "BL": "BY"}
LAST = {}  # канал -> время окончания последней передачи


def split_shift(name: str):
    """«Первый канал (+4)» → («Первый канал», 4)."""
    m = SHIFT_RE.search(name)
    if not m:
        return name, 0
    return (name[:m.start()] + " " + name[m.end():]).strip(), int(m.group(1))


# ---------------------------------------------------------------- загрузка

def download(url: str, dst_dir: str, retries: int = 3) -> str:
    path = os.path.join(dst_dir, "src.bin")
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=180) as r, open(path, "wb") as f:
                shutil.copyfileobj(r, f, 1 << 20)
            if os.path.getsize(path) < 100:
                raise IOError("пустой ответ")
            return path
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(5 * (attempt + 1))
    raise IOError(f"не удалось скачать {url}: {last}")


def open_xml(path: str):
    with open(path, "rb") as f:
        magic = f.read(6)
    if magic[:2] == b"\x1f\x8b":
        return gzip.open(path, "rb")
    if magic[:6] == b"\xfd7zXZ\x00":
        return lzma.open(path, "rb")
    return open(path, "rb")


# ---------------------------------------------------------------- время

def parse_time(s: str):
    s = s.strip()
    m = re.match(r"(\d{14})\s*([+-]\d{4})?", s)
    if not m:
        return None
    t = dt.datetime.strptime(m.group(1), "%Y%m%d%H%M%S")
    off = m.group(2) or "+0000"
    sign = 1 if off[0] == "+" else -1
    tz = dt.timezone(sign * dt.timedelta(hours=int(off[1:3]), minutes=int(off[3:5])))
    return t.replace(tzinfo=tz)


def fmt_time(t: dt.datetime) -> str:
    return t.strftime("%Y%m%d%H%M%S %z")


# ---------------------------------------------------------------- каналы плейлиста

def load_channels(path, exclude_groups):
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.startswith("#") or not line.strip():
                continue
            group, _, name = line.rstrip("\n").partition("\t")
            name = name.strip()
            if name and group not in exclude_groups:
                rows.append((group, name))
    return rows


def build_targets(rows, aliases, rewrites):
    """Для каждого канала плейлиста — набор ключей поиска и сдвиг.

    Возвращает:
      targets: name -> {"group", "shift", "keys": [(kind, key)]}
    kind: "id" (точный id в источнике), "strict", "loose"
    """
    targets = {}
    for group, name in rows:
        base, shift = split_shift(name)
        alias = aliases.get(name) or aliases.get(base)
        keys = []
        if alias:
            for a in (alias if isinstance(alias, list) else [alias]):
                if a == "-":
                    keys = [("none", "")]
                    break
                if a.startswith("id:"):
                    keys.append(("id", a[3:]))
                else:
                    keys.append(("strict", norm(a)))
                    keys.append(("loose", loose(a)))
                    keys.append(("compact", compact(a)))
        if keys and keys[0][0] == "none":
            targets[name] = {"group": group, "shift": shift, "keys": []}
            continue
        candidates = [base]
        cur = base
        for pattern, repl in rewrites:
            alt = re.sub(pattern, repl, cur, flags=re.I).strip()
            if alt != cur:
                candidates.append(alt)
                cur = alt
        m = re.match(r"([A-Z]{2})\s", group)
        if m and m.group(1) != "RU" and not re.search(r"\[[A-Za-z]{2,6}\]", base):
            code = COUNTRY.get(m.group(1), m.group(1))
            candidates += [f"{c} [{code}]" for c in list(candidates)]
        for c in candidates:
            keys.append(("strict", norm(c)))
        for c in candidates:
            keys.append(("loose", loose(c)))
        for c in candidates:
            keys.append(("compact", compact(c)))
        # без дублей, порядок важен: сначала точнее
        seen, uniq = set(), []
        for k in keys:
            if k[1] and k not in seen:
                seen.add(k)
                uniq.append(k)
        targets[name] = {"group": group, "shift": shift, "keys": uniq}
    return targets


# ---------------------------------------------------------------- обработка источника

def process_source(src, targets, pending, window, keep_desc, tmp_dir, log, dump_dir=None):
    """Читает один источник, пишет программы найденных каналов во временный файл.

    pending — множество каналов плейлиста, которым программа ещё не найдена.
    Возвращает (путь к временному файлу, {канал: число передач}, {канал: исходное имя}).
    """
    only = re.compile(src["only"], re.I) if src.get("only") else None
    wanted = [n for n in pending if not only or only.search(n)]
    if not wanted:
        return None, {}, {}

    work = tempfile.mkdtemp(dir=tmp_dir)
    path = download(src["url"], work)

    out_path = os.path.join(tmp_dir, f"prog_{len(os.listdir(tmp_dir))}.xml")
    counts = defaultdict(int)
    last = {}
    matched_name = {}
    # id канала в источнике -> [(канал плейлиста, сдвиг)]
    route = defaultdict(list)
    # индексы источника
    by_id, by_strict, by_loose, by_compact = {}, {}, {}, {}
    chan_names = {}
    all_names = []
    resolved = False
    lo, hi = window

    def resolve():
        """Когда все <channel> прочитаны — решаем, какой канал плейлиста куда идёт."""
        taken = set()
        for name in wanted:
            t = targets[name]
            for kind, key in t["keys"]:
                idx = {"id": by_id, "strict": by_strict, "loose": by_loose,
                       "compact": by_compact}[kind]
                cid = idx.get(key)
                if cid:
                    route[cid].append((name, t["shift"]))
                    matched_name[name] = chan_names.get(cid, cid)
                    taken.add(name)
                    break
        log(f"  сопоставлено каналов: {len(taken)} из {len(wanted)}")

    with open_xml(path) as fh, open(out_path, "w", encoding="utf-8") as out:
        ctx = ET.iterparse(fh, events=("start", "end"))
        _, root = next(ctx)
        for event, el in ctx:
            if event != "end":
                continue
            if el.tag == "channel":
                cid = el.get("id")
                if cid:
                    by_id.setdefault(cid, cid)
                    names = [d.text for d in el.findall("display-name") if d.text]
                    chan_names[cid] = names[0] if names else cid
                    all_names.append((cid, names))
                    for n in names + [cid]:
                        by_strict.setdefault(norm(n), cid)
                        by_loose.setdefault(loose(n), cid)
                        by_compact.setdefault(compact(n), cid)
                root.clear()
            elif el.tag == "programme":
                if not resolved:
                    resolve()
                    resolved = True
                dests = route.get(el.get("channel"))
                if dests:
                    start = parse_time(el.get("start", ""))
                    stop = parse_time(el.get("stop", "")) if el.get("stop") else None
                    if start and (stop or start) >= lo and start <= hi:
                        if not keep_desc:
                            for d in el.findall("desc"):
                                el.remove(d)
                        el.tail = None
                        for name, shift in dests:
                            delta = dt.timedelta(hours=-shift)
                            el.set("channel", name)
                            el.set("start", fmt_time(start + delta))
                            if stop:
                                el.set("stop", fmt_time(stop + delta))
                            out.write(ET.tostring(el, encoding="unicode"))
                            out.write("\n")
                            counts[name] += 1
                            end = (stop or start) + delta
                            if name not in last or end > last[name]:
                                last[name] = end
                root.clear()
    if not resolved:  # источник без передач
        resolve()
    shutil.rmtree(work, ignore_errors=True)
    if dump_dir:
        slug = re.sub(r"[^\w.-]+", "_", src["name"])
        with open(os.path.join(dump_dir, f"{slug}.tsv"), "w", encoding="utf-8") as f:
            for cid, names in all_names:
                f.write(cid + "\t" + " | ".join(names) + "\n")
    for n, t in last.items():
        LAST[n] = t
    return out_path, dict(counts), matched_name


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(HERE, "config.json"))
    ap.add_argument("--out", default=os.path.join(HERE, "public"))
    args = ap.parse_args()

    with open(args.config, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg_dir = os.path.dirname(os.path.abspath(args.config))

    def rel(p):
        return p if os.path.isabs(p) else os.path.join(cfg_dir, p)

    aliases = {}
    if os.path.exists(rel(cfg.get("aliases", "aliases.json"))):
        with open(rel(cfg.get("aliases", "aliases.json")), encoding="utf-8") as f:
            aliases = {k: v for k, v in json.load(f).items() if not k.startswith("_")}

    rows = load_channels(rel(cfg.get("channels", "channels.tsv")),
                         set(cfg.get("exclude_groups", [])))
    targets = build_targets(rows, aliases, cfg.get("name_rewrites", []))

    now = dt.datetime.now(dt.timezone.utc)
    window = (now - dt.timedelta(days=cfg.get("past_days", 7)),
              now + dt.timedelta(days=cfg.get("future_days", 8)))
    keep_desc = cfg.get("keep_desc", True)

    os.makedirs(args.out, exist_ok=True)
    dump_dir = os.path.join(args.out, "sources")
    os.makedirs(dump_dir, exist_ok=True)
    tmp_dir = tempfile.mkdtemp()
    pending = set(targets)
    filled = {}       # канал -> (источник, исходное имя, число передач)
    parts = []
    log_lines = []

    def log(msg):
        print(msg, flush=True)
        log_lines.append(msg)

    for src in cfg["sources"]:
        if src.get("enabled", True) is False:
            continue
        log(f"Источник: {src['name']}  ({src['url']})")
        try:
            part, counts, mnames = process_source(src, targets, pending, window,
                                                  keep_desc, tmp_dir, log, dump_dir)
        except Exception as e:  # noqa: BLE001
            log(f"  ОШИБКА: {e}")
            continue
        if not part:
            log("  пропущен: нечего искать")
            continue
        got = {n for n, c in counts.items() if c > 0}
        log(f"  получили программу: {len(got)} каналов")
        for n in got:
            filled[n] = (src["name"], mnames.get(n, ""), counts[n])
        pending -= got
        parts.append((part, got))

    # итоговый файл: сначала <channel>, потом передачи
    final_xml = os.path.join(tmp_dir, "epg.xml")
    with open(final_xml, "w", encoding="utf-8") as out:
        out.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        out.write(f'<tv generator-info-name="epg-builder" date={quoteattr(now.strftime("%Y%m%d%H%M%S +0000"))}>\n')
        for name in targets:
            if name not in filled:
                continue
            out.write(f"<channel id={quoteattr(name)}>")
            out.write(f"<display-name>{escape(name)}</display-name>")
            collapsed = " ".join(name.split())
            if collapsed != name:
                out.write(f"<display-name>{escape(collapsed)}</display-name>")
            out.write("</channel>\n")
        for part, got in parts:
            if not got:
                continue
            with open(part, encoding="utf-8") as fh:
                for line in fh:
                    # в часть могли попасть передачи каналов, чьё «got» пусто — их нет
                    out.write(line)
        out.write("</tv>\n")

    gz_path = os.path.join(args.out, "epg.xml.gz")
    with open(final_xml, "rb") as fi, gzip.open(gz_path, "wb", compresslevel=9) as fo:
        shutil.copyfileobj(fi, fo)

    # отчёт
    by_group = defaultdict(lambda: [0, 0])
    for name, t in targets.items():
        by_group[t["group"]][1] += 1
        if name in filled:
            by_group[t["group"]][0] += 1
    rep = [f"# Отчёт сборки EPG — {now.strftime('%Y-%m-%d %H:%M UTC')}", "",
           f"Каналов в плейлисте (без исключённых групп): {len(targets)}",
           f"С программой: {len(filled)}", f"Без программы: {len(targets) - len(filled)}",
           f"Размер epg.xml.gz: {os.path.getsize(gz_path) / 1e6:.1f} МБ", "",
           "## По группам", "", "| Группа | С программой | Всего |", "|---|---|---|"]
    for g in sorted(by_group, key=lambda g: -by_group[g][1]):
        rep.append(f"| {g} | {by_group[g][0]} | {by_group[g][1]} |")
    rep += ["", "## Без программы", ""]
    for g in sorted(by_group):
        miss = [n for n, t in targets.items() if t["group"] == g and n not in filled]
        if miss:
            rep.append(f"**{g}**: " + "; ".join(miss))
            rep.append("")
    stale = [n for n in filled if n in LAST and LAST[n] < now + dt.timedelta(hours=12)]
    if stale:
        rep += ["## Программа заканчивается в ближайшие 12 часов или уже кончилась", "",
                "; ".join(stale), ""]
    msk = dt.timezone(dt.timedelta(hours=3))
    rep += ["## Откуда что взято", "",
            "| Канал | Источник | Имя в источнике | Передач | Программа до (МСК) |",
            "|---|---|---|---|---|"]
    for name in targets:
        if name in filled:
            s, m, c = filled[name]
            until = LAST[name].astimezone(msk).strftime("%d.%m %H:%M") if name in LAST else ""
            rep.append(f"| {name} | {s} | {m} | {c} | {until} |")
    rep += ["", "## Лог", "", "```"] + log_lines + ["```", ""]
    with open(os.path.join(args.out, "report.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(rep))

    shutil.rmtree(tmp_dir, ignore_errors=True)
    print(f"\nГотово: {len(filled)}/{len(targets)} каналов с программой → {gz_path}")
    if not filled:
        sys.exit(2)


if __name__ == "__main__":
    main()
