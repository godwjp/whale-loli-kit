#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""记忆星图 —— Hindsight 本地记忆库的只读可视化。

设计约束（照实施单第 6 章）：
  * 只读：只 GET 本地 daemon 的接口，不写库、不改人格。
  * 只监听 127.0.0.1。
  * 遗忘参数与页面共用同一套，避免"页面 0.52、实际 0.38"。
  * Hindsight 没有 importance 字段，这里用「推算强度」并在页面上明确标注来源。

环境变量：
  HINDSIGHT_API   默认 http://127.0.0.1:9077
  MEM_BANK        默认 coding-agent::default（换成你自己的库名）
  BRAINVIZ_PORT   默认 8765
  BRAINVIZ_HOST   默认 127.0.0.1
"""

from __future__ import annotations

import json
import math
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

API = os.environ.get("HINDSIGHT_API", "http://127.0.0.1:9077").rstrip("/")
BANK = os.environ.get("MEM_BANK", "coding-agent::default")

# ⭐⭐ 2026-11-20（主人：「我看了一下记忆星图，我要的是小瑜的呀，不是前台的呀」）
#
#   星图原来只读 hindsight（9077）里 `coding-agent::大肥鱼` 那个库 ——
#   那是**前台（编程助手）**的记忆。小瑜的记忆是**完全另一套**，躺在
#   `%USERPROFILE%\.dsh\douyin-duty\` 下的 JSON/Markdown 里：
#       memory/dm-*.json        每个人 + 聊过什么
#       memory/facts-*.json     长期事实（带 hits 衰减）
#       memory/comment-*.json   评论记录
#       diary/YYYY-MM-DD.md     日记
#
#   ⇒ 现在支持 `?src=xiaoyu` 切到小瑜那套（同一个页面、同一套视觉，
#     只是数据源换掉）。默认仍是前台，互不影响。
XIAOYU_HOME = os.environ.get(
    "XIAOYU_HOME", os.path.join(os.path.expanduser("~"), ".dsh", "douyin-duty"))
PORT = int(os.environ.get("BRAINVIZ_PORT", "8765"))
HOST = os.environ.get("BRAINVIZ_HOST", "127.0.0.1")
HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------- 推算模型
# 全部参数集中在这里。改一处，页面和曲线同时变。
FACT_BASE = {"observation": 0.80, "world": 0.68, "experience": 0.55}
DEFAULT_BASE = 0.58
MANUAL_BONUS = 0.10      # 没有 document_id：直接写入、明确要求记住的
PROOF_BONUS = 0.04       # 每多一次"被证实"（最多算 3 次）
DEGREE_BONUS = 0.10      # 关联度（对数归一）后的上限加成
IMP_CAP = 0.96

EMOTION_WORDS = (
    "喜欢", "讨厌", "生气", "害怕", "担心", "怕", "开心", "难过", "哭", "笑",
    "爱", "烦", "焦虑", "崩溃", "失望", "后悔", "心疼", "累", "想要", "希望",
    "错误", "失败", "骂", "气死", "委屈",
)
PERSON_WORDS = ("用户", "user", "主人", "小瑜", "小鱼", "妈妈", "朋友", "助手", "作者", "创作者")

PROTECT_LINE = 0.70
RETIRE_LINE = 0.45


def imp_of(m: dict, degree: int, deg_ref: float) -> float:
    """强度：类型基准 + 显式写入 + 被证实次数 + 关联度（对数归一）。"""
    imp = FACT_BASE.get(m.get("fact_type") or "", DEFAULT_BASE)
    if not m.get("document_id"):
        imp += MANUAL_BONUS
    proofs = m.get("proof_count") or 1
    if isinstance(proofs, int) and proofs > 1:
        imp += PROOF_BONUS * min(proofs - 1, 3)
    if deg_ref > 0 and degree > 0:
        imp += DEGREE_BONUS * (math.log1p(degree) / math.log1p(deg_ref))
    return max(0.05, min(IMP_CAP, imp))


def emotion_of(m: dict) -> bool:
    """只认真正的情绪词。Hindsight 给事实加的 "| When:/| Where:" 后缀不算情绪。"""
    text = (m.get("text") or "")
    return any(w in text for w in EMOTION_WORDS)


def stability_of(imp: float, degree: int, emotion: bool) -> float:
    s = 6.0 * (0.6 + 2.4 * imp) * (1.0 + 0.9 * math.log1p(degree))
    return s * (1.2 if emotion else 1.0)


def clarity_of(age_days: float, stability: float) -> float:
    return math.exp(-age_days / max(0.5, stability))


def region_of(m: dict, imp: float, age_days: float, degree: int, emotion: bool,
              deg_p90: int, fresh_hours: float, imp_top: bool) -> str:
    """脑区：极值用分位数（保证稀有），其余用语义规则。"""
    ents = (m.get("entities") or "")
    ft = (m.get("fact_type") or "")
    if imp_top:
        return "脑干 · 世界之魂"
    # ⭐ 2026-11-20：小瑜那套（person/fact/diary/comment）按类型直接落区，
    #   否则 31 个节点全挤进"扩展联想"，看不出层次。
    if ft == "person":
        return "颞叶 · 人物"
    if ft == "fact":
        return "海马体 · 编码索引" if age_days * 24 <= fresh_hours else "顶叶 · 事实"
    if ft == "diary":
        return "视交叉上核 · 静默节律"
    if ft == "comment":
        return "前额叶 · 检索决策" if degree >= 1 else "联合皮层 · 扩展联想"
    if degree >= max(4, deg_p90):
        return "前额叶 · 检索决策"
    if emotion:
        return "杏仁核 · 情绪核心"
    if age_days * 24 <= fresh_hours:
        return "海马体 · 编码索引"
    if any(w in ents for w in PERSON_WORDS):
        return "颞叶 · 人物"
    if (m.get("fact_type") or "") == "world":
        return "顶叶 · 事实"
    if age_days > 14:
        return "视交叉上核 · 静默节律"
    return "联合皮层 · 扩展联想"


def _ring_label(d: float) -> str:
    if d * 24 < 1:
        return f"{d * 24 * 60:.0f}分"
    if d < 1:
        return f"{d * 24:g}小时"
    return f"{d:g}天"


def temp_bucket(c: float) -> str:
    if c >= 0.95:
        return "刻在心里"
    if c >= 0.88:
        return "记得清楚"
    if c >= 0.75:
        return "还新鲜"
    if c >= 0.55:
        return "有点模糊"
    if c >= 0.35:
        return "快忘了"
    return "几乎想不起"


def parse_dt(s):
    if not s:
        return None
    try:
        s = s.replace("Z", "+00:00")
        d = datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d
    except Exception:
        return None


# ---------------------------------------------------------------- 取数据
def api_get(path: str, **params) -> dict:
    q = ("?" + urllib.parse.urlencode(params)) if params else ""
    url = f"{API}/v1/default/banks/{urllib.parse.quote(BANK, safe='')}/{path}{q}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_all_memories() -> list:
    out, offset, limit = [], 0, 500
    for _ in range(20):
        d = api_get("memories/list", limit=limit, offset=offset)
        items = d.get("items") or []
        out.extend(items)
        if len(items) < limit:
            break
        offset += limit
    return out


def _read_json(p):
    try:
        with open(p, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return None


def fetch_xiaoyu():
    """把小瑜的记忆读成 (mems, edges)，形状跟 hindsight 那套一致。

    节点四类：人 / 事实 / 日记 / 评论。连线：人—事实、日记—提到的人。
    """
    import glob
    mems, edges = [], []
    now_iso = datetime.now(timezone.utc).isoformat()
    home = Path(XIAOYU_HOME)
    memdir = home / "memory"
    diarydir = home / "diary"

    # 先收集「人」，后面日记要按昵称连线
    nick_by_sec = {}

    for fp in sorted(glob.glob(str(memdir / "dm-*.json"))):
        j = _read_json(fp)
        if not isinstance(j, dict):
            continue
        sec = Path(fp).stem[3:]
        who = j.get("who") or {}
        nick = str(who.get("nick") or "").strip() or sec[:10]
        nick_by_sec[sec] = nick
        msgs = [m for m in (j.get("messages") or []) if isinstance(m, dict)]
        last = ""
        when = j.get("updatedAt") or now_iso
        for m in reversed(msgs):
            if m.get("at"):
                when = m["at"]
                last = str(m.get("text") or "")
                if last.strip():
                    break
        n_facts = len((_read_json(memdir / f"facts-{sec}.json") or {}).get("facts") or [])
        mems.append({
            "id": "person:" + sec,
            "text": f"{nick} · 聊过 {len(msgs)} 条 · 记住 {n_facts} 件事"
                    + (f" · 最近：{last[:30]}" if last.strip() else ""),
            "fact_type": "person",
            "mentioned_at": when,
        })

    for fp in sorted(glob.glob(str(memdir / "facts-*.json"))):
        j = _read_json(fp)
        if not isinstance(j, dict):
            continue
        sec = Path(fp).stem[6:]
        pid = "person:" + sec
        if not any(m["id"] == pid for m in mems):
            # 没有 dm 记录（比如渡口那个号从没同步过对话）⇒ 用 facts 文件里的 who 补一个人物节点，
            # 否则她记住的这些事会整批消失（实测丢了「断劫归心」的 3 条）。
            who = j.get("who") or {}
            nick = str(who.get("nick") or "").strip() or sec[:10]
            nick_by_sec[sec] = nick
            mems.append({
                "id": pid,
                "text": f"{nick} · 没同步过对话 · 记住 {len(j.get('facts') or [])} 件事",
                "fact_type": "person",
                "mentioned_at": now_iso,
            })
        for i, f in enumerate(j.get("facts") or []):
            if not isinstance(f, dict):
                continue
            txt = str(f.get("text") or "").strip()
            if not txt:
                continue
            fid = f"fact:{sec}:{i}"
            mems.append({
                "id": fid,
                "text": txt,
                "fact_type": "fact",
                "mentioned_at": f.get("createdAt") or now_iso,
                "proof_count": max(1, int(f.get("hits") or 1)),
            })
            edges.append({"data": {"source": pid, "target": fid, "linkType": "fact"}})

    for fp in sorted(glob.glob(str(diarydir / "20*.md"))):
        day = Path(fp).stem
        try:
            raw = Path(fp).read_text(encoding="utf-8")
        except Exception:
            continue
        body = raw.split("## 我自己写的", 1)[-1].strip()
        body = " ".join(body.split())
        if not body:
            body = "（这一天没写正文）"
        did = "diary:" + day
        mems.append({
            "id": did,
            "text": f"{day} 日记：{body[:60]}",
            "fact_type": "diary",
            "mentioned_at": f"{day}T22:00:00+08:00",
        })
        # 日记里提到谁就连过去（人物连线让星图有"回忆挂在哪个人身上"的层次）
        for sec, nick in nick_by_sec.items():
            if nick and nick in body:
                edges.append({"data": {"source": did, "target": "person:" + sec,
                                       "linkType": "recall"}})

    for fp in sorted(glob.glob(str(memdir / "comment-*.json"))):
        j = _read_json(fp)
        if not isinstance(j, dict):
            continue
        aweme = str(j.get("aweme_id") or Path(fp).stem[8:])
        for i, c in enumerate(j.get("mine") or []):
            if not isinstance(c, dict):
                continue
            txt = str(c.get("text") or "").strip()
            if not txt:
                continue
            mems.append({
                "id": f"comment:{aweme}:{i}",
                "text": f"我在 {aweme[-6:]} 下说：{txt}",
                "fact_type": "comment",
                "mentioned_at": c.get("at") or now_iso,
            })

    return mems, edges


def build(src: str = "hindsight") -> dict:
    if src == "xiaoyu":
        mems, edges = fetch_xiaoyu()
        nodes = []
        bank_name = "xiaoyu::" + os.path.basename(XIAOYU_HOME)
    else:
        graph = api_get("graph")
        nodes = graph.get("nodes") or []
        edges = graph.get("edges") or []
        mems = None
        bank_name = BANK

    degree = {}
    temporal = set()
    for e in edges:
        d = e.get("data") or e
        s, t = d.get("source"), d.get("target")
        if s and t and s != t:
            degree[s] = degree.get(s, 0) + 1
            degree[t] = degree.get(t, 0) + 1
        if d.get("linkType") == "temporal":
            temporal.add(s)
            temporal.add(t)
    degree_max = max(degree.values()) if degree else 0
    degs = sorted(degree.values()) if degree else [0]

    def pct(p):
        return degs[min(len(degs) - 1, int(len(degs) * p))]

    deg_p90 = pct(0.90)
    deg_ref = max(1, pct(0.99))

    if mems is None:
        mems = fetch_all_memories()
    if not mems and nodes:
        # graph 有节点但没有 list 时，退化成只有图节点（仍可用）
        mems = [
            {"id": (n.get("data") or n).get("id"),
             "text": (n.get("data") or n).get("text") or "",
             "fact_type": "world",
             "mentioned_at": (n.get("data") or n).get("date")}
            for n in nodes
        ]

    now = datetime.now(timezone.utc)

    # 第一遍：算年龄，才知道该用小时刻度还是天刻度
    rows = []
    for m in mems:
        mid = m.get("id")
        if not mid:
            continue
        when = parse_dt(m.get("occurred_start")) or parse_dt(m.get("mentioned_at")) \
            or parse_dt(m.get("date")) or now
        age_days = max(0.0, (now - when).total_seconds() / 86400.0)
        deg = degree.get(mid, 0)
        emo = emotion_of(m) or mid in temporal
        imp = imp_of(m, deg, deg_ref)
        stab = stability_of(imp, deg, emo)
        rows.append((m, mid, age_days, deg, emo, imp, stab, clarity_of(age_days, stab)))

    max_age = max((r[2] for r in rows), default=0.0)
    # 脑干只留真正的极值：按名次取前 2%，不能按数值 —— 大量并列会一起涌进去
    top_n = max(1, int(len(rows) * 0.02)) if rows else 0
    imp_top_ids = {r[1] for r in sorted(rows, key=lambda r: (-r[5], -r[3]))[:top_n]}

    # 自适应刻度：数据只跨几小时就用小时环，跨很久才用天/周环
    if max_age < 0.25:
        ring_days, unit = [0.05, 0.125, 0.25], "小时"
    elif max_age < 1.5:
        ring_days, unit = [0.042, 0.125, 0.5, 1.0], "小时"
    elif max_age < 8:
        ring_days, unit = [0.25, 1, 3, 7], "天"
    elif max_age < 40:
        ring_days, unit = [1, 3, 10, 30], "天"
    else:
        ring_days, unit = [1, 7, 30, 90, 180], "天"
    fresh_hours = 2.0 if max_age < 0.25 else 24.0
    rings = [{"days": d, "label": _ring_label(d)} for d in ring_days]

    items = []
    for (m, mid, age_days, deg, emo, imp, stab, cl) in rows:
        text = (m.get("text") or "").strip()
        items.append({
            "id": mid,
            "text": text,
            "short": text[:46] + ("…" if len(text) > 46 else ""),
            "age_days": round(age_days, 4),
            "r": round(120 + math.log1p(age_days * 24) * 60, 1),
            "imp": round(imp, 3),
            "stability": round(stab, 1),
            "clarity": round(cl, 4),
            "region": region_of(m, imp, age_days, deg, emo, deg_p90, fresh_hours,
                                mid in imp_top_ids),
            "temp": temp_bucket(cl),
            "degree": deg,
            "emotion": emo,
            "fact_type": m.get("fact_type") or "world",
            "angle": round((hash(mid) % 10000) / 10000.0 * 2 * math.pi, 4),
        })

    # 脑区
    regions = {}
    for it in items:
        r = regions.setdefault(it["region"], {"name": it["region"], "count": 0, "sum": 0.0})
        r["count"] += 1
        r["sum"] += it["clarity"]
    region_list = sorted(
        [{"name": v["name"], "count": v["count"],
          "avg_clarity": round(v["sum"] / v["count"], 3)} for v in regions.values()],
        key=lambda x: -x["count"],
    )

    # 体温
    temps = {}
    for it in items:
        t = temps.setdefault(it["temp"], {"name": it["temp"], "count": 0, "sum": 0.0})
        t["count"] += 1
        t["sum"] += it["clarity"]
    order = ["刻在心里", "记得清楚", "还新鲜", "有点模糊", "快忘了", "几乎想不起"]
    temp_list = [{"name": k, "count": temps.get(k, {}).get("count", 0),
                  "avg_clarity": round(temps.get(k, {}).get("sum", 0.0) / temps[k]["count"], 3)
                  if k in temps else 0.0} for k in order]

    # 遗忘曲线参考线（同一套参数）
    profiles = [
        {"name": "没被想起的小事", "imp": 0.35, "deg": 0, "emo": False},
        {"name": "想起过几次", "imp": 0.55, "deg": 3, "emo": False},
        {"name": "带情绪的", "imp": 0.65, "deg": 2, "emo": True},
        {"name": "常被想起的重要事", "imp": 0.85, "deg": 10, "emo": True},
    ]
    prof_out = []
    for p in profiles:
        stab = stability_of(p["imp"], p["deg"], p["emo"])
        pts = [[d, round(clarity_of(d, stab), 4)] for d in (0, 1, 2, 3, 5, 7, 10, 15, 20, 30, 45, 60, 90)]
        prof_out.append({"name": p["name"], "stability": round(stab, 1),
                         "points": pts})

    avg = sum(i["clarity"] for i in items) / len(items) if items else 0.0
    ages = sorted(i["age_days"] for i in items)
    med_age = ages[len(ages) // 2] if ages else 0.0
    fresh_note = ""
    if max_age < 7:
        fresh_note = (f"记忆跨度只有 {max_age * 24:.1f} 小时（中位 {med_age * 24:.1f} 小时），"
                      f"所以体温还挤在上档 —— 等积累到一两周，分层会自然拉开。")
    drop_today = sum(
        max(0.0, i["clarity"] - clarity_of(i["age_days"] + 1, i["stability"]))
        for i in items if i["imp"] < PROTECT_LINE
    )

    forgotten = sorted(
        [i for i in items if i["imp"] < PROTECT_LINE], key=lambda x: x["clarity"]
    )[:8]

    return {
        "bank": bank_name,
        "generated_at": now.isoformat(),
        "total": len(items),
        "avg_clarity": round(avg, 3),
        "protected": sum(1 for i in items if i["imp"] >= PROTECT_LINE),
        "expected_drop_today": round(drop_today, 3),
        "edges": len(edges),
        "rings": rings,
        "ring_unit": unit,
        "max_age_days": round(max_age, 4),
        "fresh_note": fresh_note,
        "regions": region_list,
        "temps": temp_list,
        "items": items,
        "decay": {"profiles": prof_out,
                  "thresholds": [PROTECT_LINE, RETIRE_LINE],
                  "scatter": [[i["age_days"], i["clarity"], i["imp"]] for i in items]},
        "forgotten": [{"id": i["id"], "short": i["short"],
                       "clarity": i["clarity"], "age_days": i["age_days"]} for i in forgotten],
        "model_note": "强度=类型基准+显式写入+被证实次数+关联度；"
                      "清晰度=exp(-天数/稳定度)，稳定度=6×(0.6+2.4×强度)×(1+0.9·ln(1+关联))×(情绪1.2)",
    }


# ---------------------------------------------------------------- 服务
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            try:
                with open(os.path.join(HERE, "index.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except FileNotFoundError:
                self._send(404, b"index.html missing", "text/plain; charset=utf-8")
            return
        if path == "/api/data":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            src = (qs.get("src") or ["hindsight"])[0]
            if src not in ("hindsight", "xiaoyu"):
                src = "hindsight"
            try:
                payload = json.dumps(build(src), ensure_ascii=False).encode("utf-8")
                self._send(200, payload, "application/json; charset=utf-8")
            except Exception as e:
                msg = json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8")
                self._send(502, msg, "application/json; charset=utf-8")
            return
        if path == "/health":
            self._send(200, b'{"ok":true}', "application/json")
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")


def main():
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"[brainviz] http://{HOST}:{PORT}  bank={BANK}  api={API}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
