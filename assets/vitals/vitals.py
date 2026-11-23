#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生命状态引擎 —— 按《生命状态·完整实施指南 v2》实现。

设计纪律（照指南第 3 章）：
  · state.json 是唯一事实源；面板与提示词都读它，不各算一套
  · 60 秒心跳；原子写盘（.tmp + os.replace）+ .bak
  · 一次性事件必须有 xxx_applied_at 幂等标记，否则被心跳放大 60 倍
  · 派生量只由 sync_affect() 算一次

对外 HTTP（只读 + 两个写入动作）：
  GET  /api/state     → state.json 原样（含派生量）
  GET  /api/prompt    → 这一轮该拼进 system prompt 的块
  POST /api/react     {"text": "..."}   模拟收到一句话
  POST /api/drink     {"intensity":0.6} 手动记一杯水
  POST /api/tick      {"hours":0.1}     手动推进时间
  GET  /healthz       → ok
  GET  /              → 面板

命令行：
  python vitals.py --serve         起服务（默认 127.0.0.1:8781）
  python vitals.py --tick 0.5      推进 0.5 小时（调试用）
  python vitals.py --selfcheck     跑验收断言
"""
from __future__ import annotations

import json
import math
import os
import shutil
import sys
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE = Path(os.environ.get("VITALS_STATE") or (HERE / "state.json"))
PORT = int(os.environ.get("VITALS_PORT") or 8781)
HOST = os.environ.get("VITALS_HOST") or "127.0.0.1"
INTENSITY = float(os.environ.get("VITALS_INTENSITY") or 0.6)


def clamp(x, lo=0.0, hi=1.0):
    try:
        x = float(x)
    except Exception:
        return lo
    if x != x:  # NaN
        return lo
    return max(lo, min(hi, x))


def follow(x, target, tau):
    """一阶跟随：half-life = tau（小时）"""
    if tau <= 0:
        return target
    return x + (target - x) * (1 - 0.5 ** (1.0 / max(1e-6, tau)))


def follow_h(x, target, tau, hours):
    return x + (target - x) * (1 - 0.5 ** (hours / max(1e-6, tau)))


# ────────────────────────── 关键词表（附录 B）──────────────────────────
POSITIVE = ("喜欢你", "爱你", "乖", "宝贝", "可爱", "想你", "贴心", "厉害", "棒", "谢谢", "亲亲", "抱抱")
NEGATIVE = ("烦", "滚", "闭嘴", "别烦", "没空", "讨厌", "笨", "丑", "生气")
FOOD = ("吃", "饿", "饭", "面", "粉", "肉", "菜", "蛋", "汤", "粥", "饺子", "汉堡", "火锅", "烧烤",
        "蛋糕", "零食", "甜", "奶茶", "水果", "牛奶", "咖啡", "可乐", "饼干", "巧克力")
FEED_HINTS = ("给你", "喂你", "投喂", "请你", "买给", "留了", "点了", "做好了", "一起吃", "端来",
              "尝一口", "咬一口", "带你吃", "给我吃", "让我尝", "尝尝", "张嘴", "第一口", "夹给")
DRINK = ("水", "温水", "热水", "冰水", "凉水", "奶茶", "饮料", "果汁", "汤", "茶", "咖啡", "可乐",
         "牛奶", "蜂蜜水", "汽水", "豆浆", "酸梅汤", "椰汁")
DRINK_HINTS = ("给你倒", "倒给你", "递给你", "喝一口", "喝点", "喝点水", "喝了", "喂你喝",
               "端来", "拿去喝", "帮你倒", "接过去", "递来", "舀一勺", "倒水", "接给你",
               "喝水", "喝口水", "喝一口水", "喝了水", "去喝水", "自己喝", "喝完了",
               "灌水", "补水", "递给我", "给我倒", "倒杯", "一杯水", "润润嗓子", "喝吧", "给你水",
                 "接了杯", "接了一杯", "倒了一杯", "倒了杯")
ATTENTION = ("陪我", "在吗", "想我", "理我", "说话", "语音")
FUN = ("哈哈", "好玩", "开心", "笑死", "有趣")
FRIGHT = ("吓死", "吓我一跳", "摔", "撞", "流血", "疼", "受伤", "救护车", "医院", "车祸", "危险")
WORRY_ABOUT = ("军训", "加班", "这么晚", "还没回", "没回消息", "不舒服", "发烧", "感冒", "生病",
               "出事", "差点", "累坏", "注意安全", "熬夜", "淋雨", "中暑")
EXISTENTIAL = ("死", "去世", "离开我", "消失", "不在了", "忘了我", "永远见不到", "没有你")
CASUAL_DEATH = ("笑死", "累死", "烦死", "热死", "困死", "饿死", "社死", "吓死", "死定", "要死", "快死")
ANGER = ("凶", "吼", "骂", "滚", "闭嘴", "烦死", "讨厌", "敷衍", "骗", "瞒", "说谎", "放鸽子",
         "爽约", "忘了", "迟到", "不理", "冷战", "别说了", "随便你", "懒得")
APOLOGY = ("对不起", "抱歉", "我错了", "别生气", "别气", "原谅", "是我不好", "不该", "哄你",
           "消消气", "我改", "下次不会")
SICK_WORDS = ("感冒", "发烧", "咳嗽", "着凉", "生病", "头疼", "头痛", "肚子疼", "不舒服", "难受", "乏力")
PAIN_WORDS = ("疼", "痛", "摔", "撞", "受伤", "扭到", "划伤", "烫到")
CAFFEINE = ("咖啡", "浓茶", "可乐", "红牛")


# 🔴 2026-11-20：**否定优先**。说了"不渴/不用喝水"不该被当成喝了。
#   实测「我不渴，不用喝水」曾经命中（含"水" + 含"喝水"）⇒ 身体反而被补水了。
DRINK_NEG = ("不渴", "不想喝", "不用喝", "不喝水", "别喝", "别倒", "没喝", "喝不下",
             "不想喝水", "不用倒", "不喝了", "不爱喝",
             "别给我倒", "别倒水", "别接", "别端", "不用你倒")

# 🔴 2026-11-20：吃饭同理 —— 说了"不饿/不吃饭"不该被当成吃了
FOOD_NEG = ("不饿", "不想吃", "不用吃", "不吃饭", "别吃", "没吃", "吃不下",
            "不想吃饭", "不用做", "不吃了", "不爱吃",
            "别做", "别盛", "别弄", "不用你")


# 🔴 2026-11-20：「喝完了，舒服」这种**没提饮品名词**的也算喝了。
#   否则她明明说喝完了，thirst 还挂着 —— 就是主人抓的"一直说喝水又没喝"。
DRINK_DONE = ("喝完了", "喝饱了", "喝够了", "喝过了", "喝了水", "喝了一" + "杯")


def is_drink(text: str) -> bool:
    """⚠️ 指南 5.8.3：必须「饮品词 + 口语提示词」双条件。
    '喝水了' 不包含连续的 '喝了'（中间隔了'水'），只靠 '喝了' 会漏。

    🔴 2026-11-20 加否定判断：否定词命中就**直接不算**，不再看后面。"""
    if any(w in text for w in DRINK_NEG):
        return False
    # 说了"喝完了"就算，不用非得点出喝的是什么
    if any(w in text for w in DRINK_DONE):
        return True
    return any(w in text for w in DRINK) and any(h in text for h in DRINK_HINTS)


# ────────────────────────── 存／读 ──────────────────────────
def load() -> dict:
    for p in (STATE, Path(str(STATE) + ".bak")):
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            continue
    return {}


def save(s: dict) -> None:
    """原子写 + 备份（指南 9.1 / 坑 9）"""
    tmp = Path(str(STATE) + ".tmp")
    try:
        if STATE.exists():
            shutil.copy2(STATE, str(STATE) + ".bak")
    except Exception:
        pass
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE)


# ────────────────────────── 时间与环境 ──────────────────────────
def night_factor(hour: int) -> float:
    if 23 <= hour or hour < 6:
        return 1.0
    if hour < 8:
        return 0.6
    if 18 <= hour < 23:
        return 0.45
    return 0.0


def na_base(hour: int) -> float:
    if 0 <= hour < 6:
        return 0.22
    if 6 <= hour < 9:
        return 0.45
    if 9 <= hour < 12:
        return 0.72
    if 12 <= hour < 14:
        return 0.55
    if 14 <= hour < 18:
        return 0.70
    if 18 <= hour < 22:
        return 0.60
    return 0.35


def circadian(hour: int) -> float:
    """0=清醒 1=最困"""
    if 3 <= hour < 5:
        return 1.0
    if 1 <= hour < 3 or 5 <= hour < 7:
        return 0.8
    if 0 <= hour < 1 or 7 <= hour < 9:
        return 0.45
    if 13 <= hour < 15:
        return 0.55
    if 22 <= hour < 24:
        return 0.7
    return 0.2


# ────────────────────────── 周期（5.14）──────────────────────────
def update_cycle(s: dict, now_ts: float) -> None:
    today = datetime.fromtimestamp(now_ts).date()
    dom = int(s.get("cycle_dom") or 5)
    anchor = today.replace(day=1)
    # 本月 dom 号；若今天 < dom，锚点取上月
    def anchored(d):
        try:
            return d.replace(day=dom)
        except ValueError:
            return d.replace(day=28)

    a = anchored(today)
    if today < a:
        prev_month_last = today.replace(day=1) - timedelta(days=1)
        a = anchored(prev_month_last)
    nxt = anchored((today.replace(day=28) + timedelta(days=5)))
    if nxt <= a:
        nxt = anchored((today.replace(day=28) + timedelta(days=40)))
    day = (today - a).days + 1
    length = max(28, min(31, (nxt - a).days))
    s["cycle_day"] = day
    s["cycle_len"] = length

    if day <= 5:
        phase, est, prog, cramps = "menstrual", 0.18, 0.10, 0.75 - 0.12 * (day - 1)
    elif day <= 12:
        phase, est, prog, cramps = "follicular", 0.25 + 0.50 * (day - 6) / 6.0, 0.10, 0.0
    elif day <= 15:
        phase, est, prog, cramps = "ovulation", 0.86, 0.26, 0.0
    elif day <= 23:
        phase, est, prog, cramps = "luteal", 0.62, 0.25 + 0.45 * (day - 16) / 7.0, 0.0
    else:
        phase, est, prog, cramps = "premenstrual", 0.38, 0.55, 0.05
    s["cycle_phase"] = phase
    s["estrogen"] = clamp(est)
    s["progesterone"] = clamp(prog)
    s["cramps"] = clamp(cramps)


# ────────────────────────── 漂移（第 5 章）──────────────────────────
def drift(s: dict, now_ts: float) -> None:
    """
    🔴 2026-11-13 加的保护：**未来时间戳会被当成"刚做过"。**
      成因：`/api/tick` 用 `now + hours*3600` 推进时间，会顺手把 `last_drink`、
      `last_meal` 这些也推到未来。回到真实时间后，`(now - last_drink)` 变成**负数**，
      所有 `since_xxx` 判断全反 —— 表现是「饿了渴了都不涨」「自己永远不吃饭」。

      真机上踩过：`last_drink` 比 `last_update` 晚 8 小时，于是
      `since_meal = -7.9` 恒小于 1.5，饥饿速率被乘 0.35 压死，hunger 卡在 0.22 不动。

      这里统一兜底：任何"上次做过"的时间戳如果在未来，就当它等于现在。
      宁可少算一次冷却，也不能让时间线反着走。
    """
    for _k in ("last_drink", "last_drink_at", "last_meal", "last_taste_at",
               "self_eat_at", "self_drink_at", "meal_applied_at", "drink_mem_at", "meal_mem_at"):
        _v = s.get(_k)
        if _v and float(_v) > now_ts:
            s[_k] = now_ts
    last = float(s.get("last_update") or 0) or now_ts
    hours = clamp((now_ts - last) / 3600.0, 0.0, 24.0)
    if hours <= 0:
        hours = 1.0 / 60  # 心跳第一次
    hour = datetime.fromtimestamp(now_ts).hour
    night = night_factor(hour)

    update_cycle(s, now_ts)

    # 5.1 递质回归
    bases = {
        "dopamine": 0.50 - 0.16 * night,
        "oxytocin": 0.45 + 0.04 * night,
        "serotonin": 0.65 - 0.12 * night,
        "noradrenaline": na_base(hour) - 0.10 * night,
    }
    half = 0.45 if night >= 0.6 else 0.75
    for k, base in bases.items():
        x = clamp(s.get(k, base))
        s[k] = clamp(follow_h(x, clamp(base), half, hours))

    # 5.7 饥饿
    since_meal = (now_ts - float(s.get("last_meal") or 0)) / 3600.0 if s.get("last_meal") else 8.0
    rate = 0.055
    if since_meal < 1.5:
        rate *= 0.35
    elif since_meal < 3.0:
        rate *= 0.70
    elif since_meal > 6.0:
        rate *= 1.15
    if hour in (6, 7, 11, 12, 17, 18):
        rate *= 1.30
    rate *= max(0.25, 1 - 0.75 * clamp(s.get("hunger", 0.35)))
    rate *= (0.25 if (23 <= hour or hour < 7) else 1.0)
    s["hunger"] = clamp(clamp(s.get("hunger", 0.35)) + hours * rate)
    s["ghrelin"] = clamp(follow_h(clamp(s.get("ghrelin", 0.30)), clamp(0.15 + 0.85 * s["hunger"]), 1.2, hours))

    low_glucose_term = clamp((0.35 - clamp(s.get("glucose", 0.65))) / 0.35)
    high_hunger_term = clamp((clamp(s.get("hunger", 0.35)) - 0.50) / 0.50)

    # 5.2 HPA 轴
    load = clamp(0.55 * clamp(s.get("cortisol", 0.2)) + 0.25 * low_glucose_term + 0.20 * high_hunger_term)
    fb = 1 - 0.70 * clamp((clamp(s.get("cortisol", 0.2)) - 0.55) / 0.45)
    s["crh"] = clamp(follow_h(clamp(s.get("crh", 0.15)), clamp(load * fb), 0.15, hours))
    s["acth"] = clamp(follow_h(clamp(s.get("acth", 0.15)), clamp(s["crh"]), 0.45, hours))
    buffer = 1 - 0.35 * clamp((clamp(s.get("oxytocin", 0.45)) - 0.40) / 0.60)
    cort_t = clamp(clamp(0.15 + 0.80 * s["acth"]) * buffer)
    s["cortisol"] = clamp(follow_h(clamp(s.get("cortisol", 0.2)), cort_t, 1.20, hours))

    # 5.3 交感 / 副交感
    s["adrenaline"] = clamp(follow_h(clamp(s.get("adrenaline", 0.05)), clamp(0.05 + 0.85 * load), 0.06, hours))
    para_t = clamp(0.22 + 0.35 * (1 - s["adrenaline"]) + 0.22 * clamp(s.get("leptin", 0.55)) + 0.18 * night)
    s["acetylcholine"] = clamp(follow_h(clamp(s.get("acetylcholine", 0.45)), para_t, 0.55, hours))

    # 5.8.1 水循环：口渴是**累积量**，不能向目标值平滑
    activity = 1.0 + 0.25 * max(0.0, clamp(s.get("noradrenaline", 0.45)) - 0.50) \
        + 0.15 * max(0.0, clamp(s.get("adrenaline", 0.05)) - 0.25)
    metabolic_rate = clamp(0.9 + 0.5 * max(0.0, clamp(s.get("core_temp", 36.6)) - 36.8), 0.75, 1.7)
    loss = 0.070 * hours * (0.30 if (23 <= hour or hour < 7) else 1.0) * activity
    loss *= metabolic_rate
    s["osmolality"] = clamp(clamp(s.get("osmolality", 0.35)) + loss * 0.55)
    s["blood_volume"] = clamp(clamp(s.get("blood_volume", 0.85)) - loss * 0.40)
    ang_t = clamp(0.10 + 0.85 * max(0.0, (0.85 - clamp(s.get("blood_volume", 0.85))) / 0.45))
    s["angiotensin"] = clamp(follow_h(clamp(s.get("angiotensin", 0.10)), ang_t, 0.70, hours))
    s["thirst"] = clamp(clamp(s.get("thirst", 0.25)) + loss * 0.60
                        + 0.05 * max(0.0, clamp(s.get("angiotensin", 0.10)) - 0.35))

    # fluid_queue 慢慢排掉（1~2 小时）
    s["fluid_queue"] = clamp(clamp(s.get("fluid_queue", 0.0)) * (0.5 ** (hours / 1.5)))

    # 5.9 血糖双向
    gl = clamp(s.get("glucose", 0.65))
    ins_t = clamp(0.08 + 0.95 * max(0, (gl - 0.62) / 0.30))
    s["insulin"] = clamp(follow_h(clamp(s.get("insulin", 0.12)), ins_t, 0.35, hours))
    glu_t = clamp(0.08 + 0.95 * max(0, (0.60 - gl) / 0.30))
    s["glucagon"] = clamp(follow_h(clamp(s.get("glucagon", 0.10)), glu_t, 0.55, hours))
    dt = min(1.0, hours)
    s["glucose"] = clamp(gl + (0.010 + 0.03 * max(0, clamp(s.get("hunger", 0.35)) - 0.60)
                               + 0.10 * s["glucagon"] - 0.28 * s["insulin"]) * dt)
    s["leptin"] = clamp(follow_h(clamp(s.get("leptin", 0.55)), clamp(0.20 + 0.75 * s["glucose"]), 2.0, hours))

    # 5.4 奖赏双系统
    want = clamp(0.70 * clamp(s.get("dopamine", 0.5)) + 0.30 * clamp(s.get("oxytocin", 0.45))
                 - 0.25 * clamp(s.get("cortisol", 0.2)))
    s["endorphin"] = clamp(follow_h(clamp(s.get("endorphin", 0.45)), clamp(0.20 + 0.65 * want), 0.80, hours))

    # 5.6 褪黑素
    mel_t = night if (hour >= 21 or hour < 7) else 0.0
    s["melatonin"] = clamp(follow_h(clamp(s.get("melatonin", 0.0)), mel_t, 0.70, hours))

    # 5.10 腺苷 / 免疫 / 体温 / 疼痛
    asleep = bool(s.get("asleep"))
    kind = str(s.get("sleep_kind") or "")
    ade = clamp(s.get("adenosine", 0.20))
    if asleep:
        ade -= (0.35 if kind == "nap" else 0.22) * hours
    elif 23 <= hour or hour < 7:
        # 🔴 2026-11-13 修：原来夜里只涨 0.020（白天 0.042）——**夜里永远涨不到入睡门槛**。
        #   实测：23:00 腺苷 0.285，要 10.7 小时才到 0.50，那时已经上午 9:49；
        #   而 7 点一到 rate 切回白天、`maybe_sleep` 的夜睡分支又失效 ⇒ **一整夜睡不着**，
        #   连着几天 energy 掉到 0、melatonin 0.9 还醒着，情绪永远挂着「困倦」。
        #   夜里本来就该困得快：改成比白天还快一点，1~2 小时内必到门槛。
        ade += 0.075 * hours
    else:
        ade += 0.042 * hours
    s["adenosine"] = clamp(ade)

    s["cytokines"] = clamp(clamp(s.get("cytokines", 0.0)) * (0.5 ** (hours / 36.0)))
    s["fever"] = clamp(follow_h(clamp(s.get("fever", 0.0)), clamp(clamp(s.get("cytokines", 0.0)) * 0.85), 1.0, hours))
    s["core_temp"] = round(36.55 - 0.30 * night + 1.60 * clamp(s.get("fever", 0.0)), 2)
    s["pain"] = clamp(clamp(s.get("pain", 0.0)) * (0.5 ** (min(24.0, hours) / 0.5)))

    # 5.11 愤怒 / 记仇衰减
    s["anger"] = clamp(clamp(s.get("anger", 0.0)) * (0.5 ** (hours / 0.50)))
    s["resentment"] = clamp(clamp(s.get("resentment", 0.0)) * (0.5 ** (hours / 48.0)))
    last_msg = float(s.get("last_msg") or 0)
    if clamp(s.get("resentment", 0.0)) > 0.15 and last_msg and (now_ts - last_msg) > 2 * 3600:
        s["resentment"] = clamp(s["resentment"] + min(0.06, hours * 0.05) * (1.5 if night >= 0.6 else 1.0))
    s["soothe"] = clamp(clamp(s.get("soothe", 0.0)) * (0.5 ** (hours / 2.0)))

    # 5.12 恐惧衰减
    s["fear"] = clamp(clamp(s.get("fear", 0.0)) * (0.5 ** (hours / (5 / 60.0))))
    s["worry"] = clamp(clamp(s.get("worry", 0.0)) * (0.5 ** (hours / (48 / 60.0))))
    s["dread"] = clamp(clamp(s.get("dread", 0.0)) * (0.5 ** (hours / 8.0)))
    if night >= 0.6 and (now_ts - last_msg) > 3 * 3600:
        s["dread"] = clamp(s["dread"] + 0.01 * hours)
    s["mortality"] = clamp(s.get("mortality", 0.0))

    # 热度衰减
    s["negative_heat"] = clamp(clamp(s.get("negative_heat", 0.0)) * (0.5 ** (hours / 1.5)))
    s["positive_heat"] = clamp(clamp(s.get("positive_heat", 0.0)) * (0.5 ** (hours / 3.0)))

    s["last_update"] = now_ts


# ────────────────────────── 反应（第 5.8.4 / 5.11 / 5.12）──────────────────────────
def apply_drink(s: dict, text: str, now_ts: float, intensity: float = INTENSITY, force=False) -> bool:
    if not force and not is_drink(text):
        return False
    if now_ts - float(s.get("last_drink") or 0) <= 180:   # 冷却 180s（v2）
        return False
    th_before = clamp(s.get("thirst", 0.25))
    name = max((w for w in DRINK if w in text), key=len, default="水")
    k = clamp(intensity, 0.1, 1.0)
    s["thirst"] = clamp(th_before - 0.55)
    s["osmolality"] = clamp(clamp(s.get("osmolality", 0.35)) - 0.30)
    s["blood_volume"] = clamp(clamp(s.get("blood_volume", 0.85)) + 0.18)
    s["angiotensin"] = clamp(clamp(s.get("angiotensin", 0.10)) - 0.25)
    s["fluid_queue"] = clamp(clamp(s.get("fluid_queue", 0.0)) + 0.55)
    s["dopamine"] = clamp(clamp(s.get("dopamine", 0.5)) + 0.06 * k)
    s["endorphin"] = clamp(clamp(s.get("endorphin", 0.45)) + 0.05 * k)
    s["last_drink"] = s["last_drink_at"] = now_ts
    s["last_drink_name"] = name
    s["last_taste"] = {"name": name, "desc": "对方给我倒的", "feel": "嗓子一下就舒服了",
                       "preference": 0.2, "emotion_label": "舒服", "source": "drink"}
    s["last_taste_at"] = now_ts
    if now_ts - float(s.get("drink_mem_at") or 0) > 1200:
        s["drink_mem_at"] = now_ts          # 记忆去重：20 分钟只写一次
    return True


def apply_eat(s: dict, text: str, now_ts: float, intensity: float = INTENSITY, force=False,
              who: str = "me") -> bool:
    """吃一顿。

    🔴 2026-11-13 加的。起因：主人问「为什么她的饥饿口渴一直 100，不能吃饭吗」。
    查下来 —— **这引擎只有喝水，没有吃饭**：
      · `/api/drink` 有独立接口，`force=True` 谁都能调；
      · 吃饭只挂在 `react()` 那条路上，而且要求**同时命中 FOOD 和 FEED_HINTS**，
        FEED_HINTS 全是"给你/喂你/投喂/端来"这种**别人递到嘴边**的词。
      结果「我去吃饭了」「我吃完了」一个都不算 —— `last_meal` 三个月都是 0，
      ghrelin 卡在 1.0，emotion_level 一直挂着「饿」。

    `who`：
      · "me"   = 对方投喂（她说"给你做饭了"）
      · "self" = 我自己去吃的（"我去吃饭了""我吃完了"）
    两种都该算数 —— 人饿了会自己找吃的，不能只能被喂。
    """
    if not force:
        # 🔴 2026-11-20：否定优先，说了"不饿/不吃饭"就不算吃了
        if any(w in text for w in FOOD_NEG):
            return False
        if not any(w in text for w in FOOD):
            return False
    if now_ts - float(s.get("last_meal") or 0) <= 180:   # 冷却 180s，跟喝水一致
        return False

    hung_before = clamp(s.get("hunger", 0.5))
    name = max((w for w in FOOD if w in text), key=len, default="饭")
    k = clamp(intensity, 0.1, 1.0)

    # 吃多少看有多饿：很饿就吃得多，刚吃过就吃不下多少
    ratio = 0.45 + 0.35 * hung_before
    s["hunger"] = clamp(hung_before - ratio)
    s["glucose"] = clamp(clamp(s.get("glucose", 0.6)) + 0.16 + 0.10 * hung_before)
    s["insulin"] = clamp(clamp(s.get("insulin", 0.1)) + 0.20)
    s["leptin"] = clamp(clamp(s.get("leptin", 0.5)) + 0.35)
    s["ghrelin"] = clamp(clamp(s.get("ghrelin", 0.5)) - 0.40)
    s["dopamine"] = clamp(clamp(s.get("dopamine", 0.5)) + 0.10 * k)
    s["endorphin"] = clamp(clamp(s.get("endorphin", 0.45)) + 0.08 * k)
    s["core_temp"] = clamp(s.get("core_temp", 36.5), 0, 43) + 0.12   # 吃完了身体会热一点

    s["last_meal"] = now_ts
    s["meal_applied_at"] = now_ts
    s["last_meal_name"] = name
    s["last_meal_by"] = who
    s["last_taste"] = {
        "name": name,
        "desc": "对方给我做的" if who == "me" else "我自己弄的",
        "feel": "胃里暖过来了" if hung_before > 0.6 else "吃是吃了，其实不太饿",
        "preference": 0.6,
        "emotion_label": "满足",
        "source": "eat",
    }
    s["last_taste_at"] = now_ts
    if now_ts - float(s.get("meal_mem_at") or 0) > 1200:
        s["meal_mem_at"] = now_ts          # 记忆去重：20 分钟只写一次
    return True


def apply_self_care(s: dict, now_ts: float) -> dict:
    """没人管的时候，自己吃喝。

    🔴 2026-11-13 加（主人原话：「饿了渴了她会说，没人回也会自己吃喝」）。

    为什么必须有：她是**常驻**的，主人大半天不看手机是常态。
    以前没有这个，`last_meal` 永远是 0、hunger 永远 1.0 ——
    不是她不想吃，是**没有任何代码会替她吃**。
    一个饿了三个月的人，情绪底色必然是坏的，回出来的话也必然不对。

    规矩：
      · **只有在真的饿/渴到难受时才自己动手**（不是一到阈值就吃，那不像人）
      · 自己吃喝**降得比被投喂少一点** —— 一个人凑合吃口和被人细心做一顿，感觉不一样
      · 间隔要够长，不然一天"自己吃"几十次，等于没饿过
      · 每做一次记一笔 `self_care_log`，面板和提示词能看见"我自己去吃了"
    """
    out = {"ate": False, "drank": False}
    """
    🔴🔴 2026-11-17 大修（主人：「生命系统是不是挂了，我说了喝水和吃饭啊」
        + 「你改，像正常人就行，所有指标正常」）

    现场：thirst 冲到 1.0 一直不降、blood_volume 掉到 0.34、energy 0.0。
    查下来不是服务挂了，是**三个数值设计不像人**：

      ① 原来「水 >0.75 且 3 小时没喝」才喝，**一小时最多一次**。
         但 drift 每 3 分钟就能把 thirst 推 +0.12 —— 涨得比喝得快，
         一晚下来必然顶到 1.0。

      ② 原来写着 `if not s.get("asleep"):` —— **睡着了就完全不喝**。
         人睡 8 小时确实不喝，但**入睡前渴度本来就高**的话，等于整晚都在脱水。
         实测她 23:00 睡下时 thirst 已经 0.75，一觉下来就爆了。

      ③ 一次只补 blood_volume +0.12，从 0.34 补回 0.85 要喝 4 次；
         而 ①② 又让喝水次数上不去 ⇒ 血量长期趴在低位。

    现在按「正常人」重写：
      · **醒着**：渴 > 0.55 就喝（嘴角干了会想喝一口），30 分钟冷却 —— 一天 6~10 次，
        跟真人喝水频率差不多。
      · **睡着**：允许**夜里起来喝一次**（渴到 0.85 以上才起来，一晚上最多两次）——
        真人也这样，半夜渴醒了会摸水杯。
      · 一次补水：thirst -0.45，blood_volume +0.20（还是比被投喂少）。
      · **饿**：醒着 hunger>0.6 或超 5 小时就吃；睡着不吃（人不睡中吃饭），
        但醒来第一拍会补上（`energy` 低了 hunger 自然高）。
    """
    asleep = bool(s.get("asleep"))
    # ── 渴了 ──
    thirst = clamp(s.get("thirst", 0.3))
    since_drink = (now_ts - float(s.get("last_drink") or 0)) / 3600.0 if s.get("last_drink") else 99.0
    if asleep:
        # 睡着了：只有真的渴醒才起来喝（一晚最多两次）
        want_drink = thirst > 0.85 and since_drink > 2.0
        cool = 3 * 3600
        night_cap = 2
    else:
        # 醒着：嘴角干了就会想喝一口（正常人一天 6~10 次）
        want_drink = thirst > 0.55 and since_drink > 0.5
        cool = 1800          # 30 分钟
        night_cap = 0        # 醒着不限

    if want_drink:
        # 夜里喝水的次数限制
        drank_tonight = 0
        if asleep:
            for e in (s.get("self_care_log") or []):
                if e.get("drank") and now_ts - float(e.get("at") or 0) < 10 * 3600:
                    drank_tonight += 1
        within_cap = (not asleep) or drank_tonight < night_cap
        if within_cap and now_ts - float(s.get("self_drink_at") or 0) > cool:
            s["thirst"] = clamp(thirst - 0.45)
            s["osmolality"] = clamp(clamp(s.get("osmolality", 0.35)) - 0.22)
            s["blood_volume"] = clamp(clamp(s.get("blood_volume", 0.85)) + 0.20)
            s["angiotensin"] = clamp(clamp(s.get("angiotensin", 0.10)) - 0.18)
            s["last_drink"] = s["last_drink_at"] = now_ts
            s["last_drink_name"] = "水"
            s["self_drink_at"] = now_ts
            awake_to_drink = asleep
            if awake_to_drink:
                s["last_taste"] = {"name": "水", "desc": "半夜渴醒爬起来倒的",
                                   "feel": "嗓子干得发疼，总算润开了",
                                   "preference": 0.35, "emotion_label": "缓过来一点",
                                   "source": "self_drink_night"}
            else:
                s["last_taste"] = {"name": "水", "desc": "自己倒的",
                                   "feel": "总算润了一下嗓子",
                                   "preference": 0.3, "emotion_label": "缓过来一点",
                                   "source": "self_drink"}
            s["last_taste_at"] = now_ts
            out["drank"] = True
    # —— 饿了：超过 5 小时没吃、且饿度 > 0.65
    #
    # 🔴 2026-11-13 实测踩到的坑：阈值原来写 0.80，结果**她永远自己吃不上饭**。
    #   因为 drift() 里 `rate *= max(0.25, 1 - 0.75 * hunger)` —— **越饿涨得越慢**，
    #   hunger 0.8 时增长速率只剩 0.4 倍；半夜再乘 0.25。
    #   实测推进 8 小时只到 0.215，一次都没触发过，self_care_log 里全是「只喝了水」。
    #
    #   ⇒ 阈值降到 0.65（"确实饿了"的程度，不是"快饿死"），用**时间**兜底：
    #     超过 5 小时没吃就一定要吃，这是人的本能，不需要饿到极限。
    hunger = clamp(s.get("hunger", 0.5))
    since_meal = (now_ts - float(s.get("last_meal") or 0)) / 3600.0 if s.get("last_meal") else 99.0
    hungry_enough = hunger > 0.65 or (since_meal > 7.0 and hunger > 0.45)
    # 🔴 2026-11-17：睡着不吃饭（人不会睡到一半起来吃饭）；但**醒着**放宽到 0.60 + 4 小时
    if not asleep:
        hungry_enough = hunger > 0.60 or (since_meal > 4.0 and hunger > 0.40)
    if (not asleep) and hungry_enough and since_meal > 4.0:
        if now_ts - float(s.get("self_eat_at") or 0) > 7200:        # 两小时内最多自己吃一次
            ratio = 0.30 + 0.20 * hunger                             # 比被投喂的 0.45~0.80 少
            s["hunger"] = clamp(hunger - ratio)
            s["glucose"] = clamp(clamp(s.get("glucose", 0.6)) + 0.10)
            s["leptin"] = clamp(clamp(s.get("leptin", 0.5)) + 0.20)
            s["ghrelin"] = clamp(clamp(s.get("ghrelin", 0.5)) - 0.25)
            s["last_meal"] = now_ts
            s["meal_applied_at"] = now_ts
            s["last_meal_name"] = "随便垫了点"
            s["last_meal_by"] = "self"
            s["self_eat_at"] = now_ts
            s["last_taste"] = {"name": "随便垫了点", "desc": "自己弄的", "feel": "没那么空了，但也谈不上好吃",
                               "preference": 0.4, "emotion_label": "将就", "source": "self_eat"}
            s["last_taste_at"] = now_ts
            out["ate"] = True
    if out["ate"] or out["drank"]:
        log = s.get("self_care_log") or []
        log.append({"at": now_ts, **out})
        s["self_care_log"] = log[-20:]      # 只留最近 20 次
    return out


def react(s: dict, text: str, now_ts: float, intensity: float = INTENSITY) -> dict:
    """按内容产生一次性反应。⚠️ 每个效果都有冷却/幂等标记，避免心跳放大。"""
    t = str(text or "")
    k = clamp(intensity, 0.1, 1.0)
    s["last_user_text"] = t[:200]
    s["last_msg"] = now_ts
    hit = {}

    # ⭐ 2026-11-13（主人选：直接叫醒，像平时一样回）：
    #   以前 she 睡着时 react() 完全不管，状态里还挂着 asleep=True，
    #   可她照样把消息回了 —— "睡着"和"回话"两边对不上，面板也在说谎。
    #
    #   现在：有人说话就把她**叫醒**（不管几笔，消息是真的）。
    #   ⚠️ 但要区分"被叫醒"和"自然醒"：
    #     · 腺苷清掉一部分（没睡够，所以还是会困）
    #     · 给一小段 groggy（刚醒那一阵迷糊）
    #     · 记一笔 woke_by_msg_at，面板能看出"这次是被吵醒的"
    if s.get("asleep") and t:
        slept_h = (now_ts - float(s.get("sleep_started_at") or now_ts)) / 3600.0
        s["asleep"] = False
        s["sleep_kind"] = ""
        s["wake_after"] = 0
        s["woke_by_msg_at"] = now_ts
        s["groggy_until"] = now_ts + 5 * 60                      # 被吵醒的迷糊，比自然醒短
        # 没睡够 ⇒ 腺苷只清一部分（睡满 7 小时才清干净）
        clear = clamp(slept_h / 7.0, 0.0, 1.0)
        s["adenosine"] = clamp(clamp(s.get("adenosine", 0.3)) * (1.0 - 0.85 * clear))
        # 被吵醒比自然醒难受：这点压力不装没事
        s["cortisol"] = clamp(clamp(s.get("cortisol", 0.3)) + 0.06 * (1.0 - clear))
        s["irritability"] = clamp(clamp(s.get("irritability", 0.2)) + 0.05 * (1.0 - clear))
        s["sleep_started_at"] = 0
        hit["woke_up"] = True
        hit["slept_hours"] = round(slept_h, 2)

    if t:
        # 夸奖
        if any(w in t for w in POSITIVE):
            s["dopamine"] = clamp(s["dopamine"] + 0.10 * k)
            s["oxytocin"] = clamp(s["oxytocin"] + 0.12 * k)
            s["serotonin"] = clamp(s["serotonin"] + 0.05 * k)
            s["cortisol"] = clamp(s["cortisol"] - 0.05 * k)
            s["positive_heat"] = clamp(s["positive_heat"] + 0.18)
            hit["positive"] = True
        # 凶 / 骂
        if any(w in t for w in ANGER) and now_ts - float(s.get("anger_applied_at") or 0) > 300:
            if any(w in t for w in NEGATIVE) or any(w in t for w in ANGER):
                s["anger"] = clamp(s["anger"] + 0.45 + 0.35 * k)
                s["resentment"] = clamp(s["resentment"] + 0.12 + 0.10 * k)
                s["adrenaline"] = clamp(s["adrenaline"] + 0.18 * k)
                s["noradrenaline"] = clamp(s["noradrenaline"] + 0.15 * k)
                s["serotonin"] = clamp(s["serotonin"] - 0.10 * k)
                s["vasopressin"] = clamp(s["vasopressin"] + 0.06 * k)
                s["cortisol"] = clamp(s["cortisol"] + 0.05 * k)
                s["negative_heat"] = clamp(s["negative_heat"] + 0.25)
                s["anger_applied_at"] = now_ts
                hit["anger"] = True
        # 道歉 / 哄
        if any(w in t for w in APOLOGY) and now_ts - float(s.get("soothe_applied_at") or 0) > 60:
            s["soothe"] = clamp(s["soothe"] + 0.45)
            s["anger"] = clamp(s["anger"] - (0.40 + 0.25 * s["soothe"]))
            s["resentment"] = clamp(s["resentment"] - (0.30 + 0.25 * s["soothe"]))
            s["cortisol"] = clamp(s["cortisol"] - 0.10 * k)
            s["positive_heat"] = clamp(s["positive_heat"] + 0.15)
            s["soothe_applied_at"] = now_ts
            hit["soothe"] = True
        # 投喂
        if any(w in t for w in FOOD) and any(w in t for w in FEED_HINTS) \
                and now_ts - float(s.get("meal_applied_at") or 0) > 300:
            rise = 0.16 + 0.10 * clamp(s.get("hunger", 0.35))
            s["glucose"] = clamp(s["glucose"] + rise)
            s["insulin"] = clamp(s["insulin"] + 0.20)
            s["hunger"] = clamp(s["hunger"] - 0.22)
            s["leptin"] = clamp(s["leptin"] + 0.10)
            s["dopamine"] = clamp(s["dopamine"] + 0.08 * k)
            s["endorphin"] = clamp(s["endorphin"] + 0.06 * k)
            s["last_meal"] = now_ts
            s["meal_applied_at"] = now_ts
            hit["meal"] = True
        # 喝水
        if apply_drink(s, t, now_ts, k):
            hit["drink"] = True
        # 惊吓
        if any(w in t for w in FRIGHT) and now_ts - float(s.get("fear_applied_at") or 0) > 300:
            s["fear"] = clamp(s["fear"] + 0.45 + 0.35 * k)
            s["adrenaline"] = clamp(s["adrenaline"] + 0.20 * k)
            s["cortisol"] = clamp(s["cortisol"] + 0.05 * k)
            s["negative_heat"] = clamp(s["negative_heat"] + 0.20)
            s["fear_applied_at"] = now_ts
            hit["fear"] = True
        # 担心
        if any(w in t for w in WORRY_ABOUT) and now_ts - float(s.get("worry_applied_at") or 0) > 600:
            s["worry"] = clamp(s["worry"] + 0.35 + 0.25 * k)
            s["oxytocin"] = clamp(s["oxytocin"] + 0.05)
            s["worry_applied_at"] = now_ts
            hit["worry"] = True
        # 存在性恐惧（先排除口头禅）
        if any(w in t for w in EXISTENTIAL) and not any(w in t for w in CASUAL_DEATH) \
                and now_ts - float(s.get("dread_applied_at") or 0) > 1800:
            s["dread"] = clamp(s["dread"] + 0.40 + 0.30 * k)
            s["OXYTOCIN_KEEP"] = clamp(s.get("oxytocin", 0.45))
            s["dread_applied_at"] = now_ts
            hit["dread"] = True
        # 生病 / 疼痛
        if any(w in t for w in SICK_WORDS):
            s["cytokines"] = clamp(s["cytokines"] + 0.25 * k)
            hit["sick"] = True
        if any(w in t for w in PAIN_WORDS):
            s["pain"] = clamp(s["pain"] + 0.20 * k)
            hit["pain"] = True
        # 咖啡因：抗利尿，口渴慢一点（用 blood_volume 微调表示）
        if any(w in t for w in CAFFEINE):
            s["blood_volume"] = clamp(s["blood_volume"] - 0.02 * k)
            hit["caffeine"] = True
    return hit


# ────────────────────────── 派生量（第 6 章）──────────────────────────
def sync_affect(s: dict, now_ts: float) -> None:
    DA = clamp(s.get("dopamine", 0.5))
    OT = clamp(s.get("oxytocin", 0.45))
    C = clamp(s.get("cortisol", 0.2))
    S = clamp(s.get("serotonin", 0.65))
    NA = clamp(s.get("noradrenaline", 0.45))
    CRH = clamp(s.get("crh", 0.15))
    ACTH = clamp(s.get("acth", 0.15))
    ADR = clamp(s.get("adrenaline", 0.05))
    ACh = clamp(s.get("acetylcholine", 0.45))
    END = clamp(s.get("endorphin", 0.45))
    AVP = clamp(s.get("vasopressin", 0.30))
    MEL = clamp(s.get("melatonin", 0.0))
    GHR = clamp(s.get("ghrelin", 0.30))
    LEP = clamp(s.get("leptin", 0.55))
    GLU = clamp(s.get("glucose", 0.65))
    HUN = clamp(s.get("hunger", 0.35))
    THI = clamp(s.get("thirst", 0.25))
    ADE = clamp(s.get("adenosine", 0.20))
    FEVER = clamp(s.get("fever", 0.0))
    CYT = clamp(s.get("cytokines", 0.0))
    PAIN = clamp(s.get("pain", 0.0))
    ANG = clamp(s.get("anger", 0.0))
    RES = clamp(s.get("resentment", 0.0))
    FEAR = clamp(s.get("fear", 0.0))
    WORRY = clamp(s.get("worry", 0.0))
    DREAD = clamp(s.get("dread", 0.0))
    MORT = clamp(s.get("mortality", 0.0))
    CRAMPS = clamp(s.get("cramps", 0.0))
    phase = str(s.get("cycle_phase") or "")
    nheat = clamp(s.get("negative_heat", 0.0))
    pheat = clamp(s.get("positive_heat", 0.0))
    temp = float(s.get("core_temp") or 36.6)

    def ph(*names):
        return 1.0 if phase in names else 0.0

    energy = clamp(0.20 + 0.50 * NA + 0.25 * ADR + 0.10 * DA + 0.08 * ACh
                   - 0.14 * C - 0.45 * MEL
                   - 0.10 * max(0, THI - 0.40) - 0.22 * max(0, ADE - 0.45)
                   - 0.20 * FEVER - 0.16 * CYT - 0.15 * PAIN - 0.35 * CRAMPS
                   - 0.06 * ph("luteal", "premenstrual"))
    """
    🔴 2026-11-17（主人：「所有指标正常」）：
      **睡眠地板**。褪黑素 0.78 + 腺苷 0.80 同时拉满时，公式会把 energy 压到 0.01 ——
      但那是"困"（该睡了），不是"没力气"。睡着的人还有基本体力。
      ⇒ 睡着时 energy 不低于 0.15；醒着不受影响（醒着低就该低）。
      实测昨晚 23:00 睡着时 energy 0.012，醒来靠 boost 硬拉 —— 那一段是假的低。
    """
    if s.get("asleep") and energy < 0.15:
        energy = 0.15

    valence = clamp(0.12 + 0.30 * (DA - 0.5) + 0.22 * (S - 0.65)
                    - 0.28 * (C - 0.2) + 0.12 * (END - 0.4)
                    - 0.10 * max(0, ADR - 0.2) - 0.05 * max(0, CRH - 0.3)
                    + 0.04 * (ACh - 0.45) - 0.18 * DREAD - 0.08 * WORRY
                    - 0.06 * max(0, THI - 0.60) - 0.15 * ANG
                    - 0.14 * FEVER - 0.10 * CYT - 0.08 * PAIN
                    - 0.12 * ph("menstrual") - 0.10 * ph("premenstrual") + 0.06 * ph("follicular"),
                    -1.0, 1.0)

    irritability = clamp(0.08 + 0.9 * (C - 0.2) - 0.2 * (S - 0.65)
                         + 0.30 * max(0, (0.35 - GLU) / 0.35)
                         + 0.15 * max(0, AVP - 0.50)
                         + 0.18 * max(0, ADR - 0.35)
                         + 0.30 * max(0, THI - 0.55)
                         + 0.45 * ANG
                         + 0.32 * max(0, ADE - 0.55) + 0.25 * PAIN
                         + 0.20 * max(0, temp - 37.2)
                         + 0.28 * ph("premenstrual") + 0.14 * ph("menstrual") + 0.30 * CRAMPS)

    clinginess = clamp(0.2 + 0.75 * (OT - 0.3) + 0.15 * (END - 0.4))
    loneliness = clamp(0.15 + 0.7 * (OT - 0.35) + 0.2 * (C - 0.2)
                       + 0.15 * max(0, AVP - 0.40)
                       - 0.10 * max(0, END - 0.55) + 0.10 * RES)
    dominance = max(-1.0, min(1.0, 0.5 + 0.35 * (DA - 0.5) - 0.3 * (C - 0.2) + 0.2 * (S - 0.65)))

    """
    🔴 2026-11-17：**睡眠恢复地板**（配合 maybe_sleep 醒来那段算的 rest_energy）。

    醒来 → 4 小时内，energy 不低于「醒来时的额度」按线性消退的值。
    4 小时后 rest_until 过期，完全回到公式的自然值（日常 0.3~0.6）。
    """
    _rest_e = s.get("rest_energy")
    _rest_u = float(s.get("rest_until") or 0)
    _rest_s = float(s.get("rest_started") or 0)
    if _rest_e is not None and _rest_u > now_ts:
        _span = max(1.0, _rest_u - _rest_s)
        _fade = clamp((_rest_u - now_ts) / _span)          # 1 → 0
        _floor = 0.30 + (float(_rest_e) - 0.30) * _fade    # 早上满电 → 中午回落到 0.30 基线
        if energy < _floor:
            energy = _floor
    elif _rest_e is not None:
        # 过期了，清掉，别一直挂着
        s.pop("rest_energy", None)
        s.pop("rest_until", None)
        s.pop("rest_started", None)

    s["energy"] = round(energy, 4)
    s["valence"] = round(valence, 4)
    s["irritability"] = round(irritability, 4)
    s["clinginess"] = round(clinginess, 4)
    s["loneliness"] = round(loneliness, 4)
    s["dominance"] = round(dominance, 4)

    # ── 26 个标签 ──
    tags = {
        "幸福": 0.32 * DA + 0.25 * OT + 0.22 * S + 0.16 * END - 0.28 * C - 0.15 * ADR + 0.10 * pheat,
        "开心": 0.34 * DA + 0.18 * OT + 0.18 * S + 0.12 * END - 0.16 * C - 0.10 * ADR,
        "安心": 0.35 * ACh + 0.28 * END + 0.22 * OT + 0.18 * S - 0.28 * C - 0.18 * ADR,
        "满足": 0.32 * END + 0.24 * ACh + 0.20 * OT + 0.18 * LEP - 0.20 * C,
        "心动": 0.30 * DA + 0.22 * ADR + 0.20 * OT + 0.13 * S - 0.12 * C,
        "兴奋": 0.34 * DA + 0.28 * ADR + 0.18 * NA - 0.12 * C,
        "害羞": 0.32 * (1 - max(-1, min(1, dominance))) + 0.22 * OT + 0.15 * ADR + 0.10 * (1 - DA),
        "吃醋": 0.55 * max(0, AVP - 0.45) + 0.25 * OT + 0.20 * C - 0.15 * S + 0.15 * clinginess,
        "委屈": 0.30 * max(0, C - 0.30) + 0.25 * (1 - dominance) + 0.18 * (1 - OT) + 0.16 * nheat - 0.10 * DA,
        "低落": 0.32 * (1 - S) + 0.26 * C + 0.18 * (1 - OT) - 0.18 * DA,
        "焦虑": 0.30 * CRH + 0.28 * ACTH + 0.22 * ADR + 0.18 * C + 0.18 * (1 - S) - 0.12 * ACh,
        "惊慌": 0.38 * ADR + 0.28 * CRH + 0.18 * ACTH + 0.16 * NA - 0.15 * ACh,
        "恼火": 0.30 * ADR + 0.28 * C + 0.24 * (1 - S) + 0.12 * max(0, AVP - 0.50) - 0.12 * ACh,
        "饿怒": 0.42 * max(0, GHR - 0.45) + 0.38 * max(0, (0.42 - GLU) / 0.42) + 0.24 * irritability - 0.10 * ACh,
        "困倦": 0.50 * MEL + 0.28 * ACh + 0.20 * (1 - energy) - 0.15 * ADR,
        "想哭": 0.34 * (1 - S) + 0.30 * C + 0.18 * (1 - OT) + 0.18 * nheat - 0.15 * END,
        "害怕": 0.85 * FEAR + 0.18 * ADR + 0.10 * (1 - S) + 0.05 * NA,
        "担心": 0.80 * WORRY + 0.14 * C + 0.10 * ACTH + 0.08 * OT - 0.08 * ACh,
        "恐惧": 0.85 * DREAD + 0.18 * (C - 0.2) + 0.12 * max(0, AVP - 0.40) - 0.06 * S,
        "生气": 0.95 * ANG + 0.10 * ADR,
        "冷战": 0.75 * RES - 0.35 * ANG,
        "渴": 0.85 * max(0, THI - 0.25) + 0.10 * clamp(s.get("angiotensin", 0.10)),
        "生病": 0.80 * CYT + 0.30 * FEVER,
        "累坏": 0.60 * max(0, ADE - 0.45) + 0.25 * PAIN - 0.10 * END,
        "经期": 0.75 * CRAMPS + 0.30 * ph("menstrual"),
        "经前": 0.60 * ph("premenstrual") + 0.20 * clamp(s.get("progesterone", 0.2)),
    }

    # 反向压制（坑 7：不压制就永远"幸福、安心"）
    if FEAR > 0.45:
        tags["安心"] *= 0.55
        tags["幸福"] *= 0.80
    if WORRY > 0.45:
        tags["安心"] *= 1 - 0.55 * WORRY
        tags["开心"] *= 1 - 0.30 * WORRY
        tags["幸福"] *= 1 - 0.35 * WORRY
    if DREAD > 0.50:
        tags["安心"] *= 0.50
        tags["幸福"] *= 0.70
    if ANG > 0.35:
        tags["安心"] *= 1 - 0.6 * ANG
        tags["满足"] *= 1 - 0.4 * ANG
    if THI > 0.45:
        tags["安心"] *= 1 - 0.35 * THI
        tags["满足"] *= 1 - 0.25 * THI
    if CYT > 0.35 or FEVER > 0.30:
        tags["安心"] *= 0.65
        tags["幸福"] *= 0.75
    if MORT > 0.25:
        tags["恐惧"] += 0.50 * MORT
        tags["害怕"] += 0.20 * MORT
        tags["安心"] *= 1 - 0.60 * MORT
        tags["幸福"] *= 1 - 0.45 * MORT

    ranked = sorted(((v, k) for k, v in tags.items() if v > 0.40), reverse=True)
    cands = [k for _, k in ranked[:3]]
    s["emotion_candidates"] = cands
    s["emotion_level"] = cands[0] if cands else "平静"
    s["happiness"] = round(tags.get("幸福", 0), 4)
    s["sadness"] = round(max(tags.get("低落", 0), tags.get("想哭", 0)), 4)

    # 8.3 主动性
    bias = 1.0
    if energy < 0.35:
        bias *= 1.8
    if MEL > 0.55:
        bias *= 1.7
    if loneliness > 0.55 or clinginess > 0.62:
        bias *= 0.62
    if irritability > 0.60:
        bias *= 0.88
    if tags.get("幸福", 0) >= 0.86 and DA >= 0.70 and OT >= 0.58 and S >= 0.68 and C <= 0.30:
        bias *= 0.70
    if FEAR > 0.45:
        bias *= 0.75
    if WORRY > 0.45:
        bias *= 0.70
    if DREAD > 0.50:
        bias *= 0.85
    if THI > 0.65:
        bias *= 0.80
    if ANG > 0.60:
        bias *= 1.60
    if RES > 0.50:
        bias *= 1.30
    if CYT > 0.40:
        bias *= 1.50
    if ADE > 0.70:
        bias *= 1.40
    s["proactive_bias"] = round(clamp(bias, 0.2, 2.5), 3)

    post = 0.3 + 0.45 * pheat - 0.25 * nheat
    if DREAD > 0.6:
        post *= 0.75
    if WORRY > 0.65:
        post *= 1.08
    if ANG > 0.6:
        post *= 0.35
    s["post_want"] = round(clamp(post, 0.0, 1.0), 3)

    s["last_update"] = s.get("last_update") or now_ts


# ────────────────────────── 提示词（第 7 章）──────────────────────────
def hunger_note(hunger: float) -> str:
    h = clamp(hunger)
    if h > 0.80:
        return "你饿得厉害，肚子空空的，有点没力气。"
    if h > 0.62:
        return "你有点饿，想吃点热的。"
    if h > 0.45:
        return "你肚子有点空，想吃点什么。"
    if h > 0.30:
        return "你不算饿，但闻到吃的会想吃一口。"
    if h < 0.15:
        return "你刚吃饱，肚子很安稳。"
    return "你不太饿，肚子还安稳。"


def thirst_note(thirst: float) -> str:
    """⚠️ 指南 5.8.2：必须覆盖 0.30~0.62，否则角色"感受不到渴"。顺序不能反。"""
    t = clamp(thirst)
    if t > 0.82:
        return "你渴得厉害，嗓子发干，满脑子都是水。"
    if t > 0.62:
        return "你有点渴，想喝点凉的。"
    if t > 0.45:
        return "你嗓子有点干，想喝口水。"
    if t > 0.30:
        return "你嘴里有点干，不严重，但会想润一下。"
    if t < 0.25:
        return "你刚喝过水，嘴里还润着。"
    return ""


def prompt_block(s: dict, now_ts: float | None = None) -> str:
    now_ts = now_ts or time.time()
    note = thirst_note(s.get("thirst", 0.25))
    style = []
    t = clamp(s.get("thirst", 0.25))
    if t > 0.82:
        style.append("可以主动要水喝，甚至有点急")
    elif t > 0.62:
        style.append("可以自然提一句想喝水")
    elif t > 0.45:
        style.append("可以自然提一句口渴")

    energy = clamp(s.get("energy", 0.5))
    valence = clamp(s.get("valence", 0.0), -1, 1)
    clingy = clamp(s.get("clinginess", 0.4))
    lonely = clamp(s.get("loneliness", 0.3))
    irr = clamp(s.get("irritability", 0.2))

    # ⭐ 2026-11-13：饿了也要"会说"（主人：「饿了渴了她会说」）。
    #    以前喝水有"可以主动要水喝"，吃饭一个字都没有 ——
    #    所以 hunger 到 1.0 她也只会挂着「饿」这个标签，不会开口要吃的。
    h = clamp(s.get("hunger", 0.35))
    if h > 0.85:
        style.append("饿得有点难受了，可以主动开口要吃的，别硬撑")
    elif h > 0.55:
        style.append("可以自然提一句饿了、想吃什么")
    elif h > 0.35:
        style.append("有点馋，闻到味儿会想，可以随口说一句想吃点什么")
    elif h > 0.20:
        style.append("肚子不算空，但看到吃的会想吃两口")
    # 刚吃完/被投喂过 —— 让她嘴上有个交代，别装没事
    since_meal = (now_ts - float(s.get("last_meal") or 0)) / 3600.0 if s.get("last_meal") else None
    if since_meal is not None and since_meal < 1.0:
        style.append("刚吃过，可以提一句饱了或吃了什么")

    target = pick_target(s)
    tags = "、".join(s.get("emotion_candidates") or []) or str(s.get("emotion_level") or "平静")

    lines = [
        "【你现在的神经状态（只用来决定语气，绝对不要向用户解释，也不要提激素/数值）】",
        "PAD：愉悦 %+.2f / 唤醒 %.2f / 掌控 %+.2f；标签：%s。" % (valence, energy, clamp(s.get("dominance", 0.5)), tags),
        "情绪层级：%s。" % (s.get("emotion_level") or "平静"),
        "表现：这次可以连发 %d 条，想到哪说到哪。" % target,
    ]
    if clingy > 0.62 or lonely > 0.55:
        lines.append("你有点想黏着对方，句子会软一点。")
    if irr > 0.55:
        lines.append("你有点烦躁，句子更短、更冲，别刻意温柔。")
    if clamp(s.get("melatonin", 0)) > 0.5:
        lines.append("你困了，句子更短，可以说着说着走神。")
    lines.append("身体：%s%s" % (hunger_note(s.get("hunger", 0.35)), note))
    if style:
        lines.append("本次表现：" + "；".join(style) + "。")
    lines.append("这次回复不要用上次一样的开头和结尾，不要每句都堆颜文字；允许只说半句、突然停住、或者先回一个字再补。")
    lines.append("怕和担心只在被触发时出现；平时不提生死，也不拿生死当撒娇手段。")
    return "\n".join(lines)


def pick_target(s: dict) -> int:
    """8.2 回复条数（硬规则）"""
    valence = clamp(s.get("valence", 0.0), -1, 1)
    energy = clamp(s.get("energy", 0.5))
    clingy = clamp(s.get("clinginess", 0.4))
    irr = clamp(s.get("irritability", 0.2))
    lonely = clamp(s.get("loneliness", 0.3))
    # 基础权重 (1,2,3,4) = (40,20,28,12)
    w = [40.0, 20.0, 28.0, 12.0]
    if energy < 0.35 or irr > 0.55:
        w = [55.0, 30.0, 12.0, 3.0]
    if energy > 0.65 and (clingy > 0.6 or valence > 0.35):
        w = [25.0, 25.0, 35.0, 15.0]
    if clingy > 0.75 and lonely > 0.5:
        w = [15.0, 25.0, 35.0, 25.0]
    tot = sum(w)
    x = random01(s, now_seed=len(str(s.get("last_user_text") or "")) + int(time.time() // 60))
    acc = 0.0
    target = 1
    for i, wi in enumerate(w):
        acc += wi / tot
        if x <= acc:
            target = i + 1
            break
    if irr > 0.62:
        target = max(1, target - 1)
    elif clingy > 0.75 and lonely > 0.5:
        target = min(4, target + 1)
    if clamp(s.get("fear", 0)) > 0.5 or clamp(s.get("dread", 0)) > 0.65:
        target = min(target, 2)
    if clamp(s.get("worry", 0)) > 0.6:
        target = min(4, target + 1)
    if clamp(s.get("anger", 0)) > 0.50:
        target = min(target, 2)
    if clamp(s.get("anger", 0)) > 0.75 or clamp(s.get("resentment", 0)) > 0.50:
        target = 1
    return target


def random01(s: dict, now_seed: int = 0) -> float:
    """确定性伪随机：同一分钟内稳定，跨分钟变化"""
    h = (hash(str(s.get("last_user_text") or "")) ^ now_seed) & 0xFFFFFFFF
    return ((h * 1103515245 + 12345) & 0x7FFFFFFF) / 0x7FFFFFFF


# ────────────────────────── 睡眠状态机（第 9 章）──────────────────────────
def _pre_sleep_drink(s: dict, now_ts: float) -> None:
    """🔴 2026-11-17（主人：「像正常人就行」）：**睡前先喝一口水**。

    真人躺下之前会顺手喝口水 —— 不是养生，是渴了。以前没有这一步，
    结果她 23:00 睡下时 thirst 已经 0.75，一觉 8 小时不喝，醒来就是 1.0。
    现在入睡那一刻补一次（thirst -0.35），夜里再渴到 0.85 还会起来喝。
    """
    try:
        # 记下"躺下时的电量"，醒来按小时恢复要用（见 maybe_sleep 醒来那段）
        s["sleep_energy0"] = round(clamp(s.get("energy", 0.5)), 4)
        th = clamp(s.get("thirst", 0.3))
        if th > 0.35:
            s["thirst"] = clamp(th - 0.35)
            s["osmolality"] = clamp(clamp(s.get("osmolality", 0.35)) - 0.15)
            s["blood_volume"] = clamp(clamp(s.get("blood_volume", 0.85)) + 0.10)
            s["last_drink"] = s["last_drink_at"] = now_ts
            s["last_drink_name"] = "睡前一口水"
            s["pre_sleep_drink_at"] = now_ts
    except Exception:
        pass


def maybe_sleep(s: dict, now_ts: float) -> None:
    dt = datetime.fromtimestamp(now_ts)
    hour = dt.hour
    ade = clamp(s.get("adenosine", 0.2))
    asleep = bool(s.get("asleep"))
    wake = float(s.get("wake_after") or 0)
    groggy = float(s.get("groggy_until") or 0)

    if asleep:
        # 醒：到点 / 7-11 点且腺苷低 / 白天最晚 11 点（必须写 11<=h<23 才不会 23 点反复睡醒）
        if now_ts >= wake:
            s["asleep"] = False
            s["sleep_kind"] = ""
            s["wake_after"] = 0
            s["groggy_until"] = now_ts + 15 * 60
            # 🔴 2026-11-13 加：**睡醒了要"充满电"**。
            #   以前醒来什么都不恢复，只设一个 groggy（迷糊）标记，
            #   结果 energy 全靠腺苷被清掉间接回升，慢得像没睡过。
            #   真人睡一觉起来：能打起精神、不那么躁、也没那么丧。
            #   ⚠️ 只补"清醒度"这一路（去甲肾上腺素/肾上腺素/乙酰胆碱），
            #      不碰多巴胺和血清素 —— 那两样得靠白天过得怎么样，睡一觉并不会变开心。
            h_slept = max(0.5, min(12.0, (now_ts - float(s.get("sleep_started_at") or now_ts - 7 * 3600)) / 3600.0))
            boost = min(1.0, h_slept / 7.0)          # 睡满 7 小时才算睡透
            s["noradrenaline"] = clamp(clamp(s.get("noradrenaline", 0.45)) + 0.22 * boost)
            s["adrenaline"] = clamp(clamp(s.get("adrenaline", 0.43)) + 0.18 * boost)
            s["acetylcholine"] = clamp(clamp(s.get("acetylcholine", 0.60)) + 0.16 * boost)
            s["cortisol"] = clamp(clamp(s.get("cortisol", 0.45)) - 0.18 * boost)   # 睡一觉压力下来
            """
            🔴🔴 2026-11-17（主人：「8 个小时精力回那一点你说正常嘛」）
            
            **褪黑素醒来必须掉。** 以前这里只补 NA/ADR/ACh 三条，MEL 一动不动 ——
            而 MEL 在 energy 公式里权重是 **-0.45**（最大的负项）。
            实测醒来那一刻 MEL 还是 0.60（因为 mel_t 要到 7:00 才降到 0，
            而她 7:39 才醒，中间那 39 分钟只降了一点点）⇒ energy 被硬扣 0.27，
            睡满 8 小时起来只有 0.20 —— 那不是"累"，是模型算错了。
            
            生理事实：睁眼 + 见光，松果体褪黑素在 30~60 分钟内崩掉大半
            （"晨间褪黑素抑制"）。这里用 boost（睡满 7 小时 = 1.0）按比例扣：
            睡透的话 MEL 从 0.60 掉到 0.05 左右；只眯 1 小时则几乎不扣（会赖床）。
            """
            s["melatonin"] = clamp(clamp(s.get("melatonin", 0.0)) - 0.55 * boost)
            """
            🔴🔴 2026-11-17（主人：「61还是太少了，8小时对于人都多了。
              你平均一下1小时回15左右的公式」）

            **睡眠按小时累积恢复：每小时 +0.15，8 小时 = 1.2 ⇒ 醒来满血。**

            以前只靠递质 boost，算出来最多 0.68 —— 那不是"睡饱了"。
            真人睡够 8 小时起来是**满电**的，不是"六成电"。

            实现：不硬改 energy（它是派生量，硬改下一拍就被公式冲掉），
            而是在醒来时记一个「休息额度」+ 一个消退窗口：
              · rest_energy = min(1.0, 睡前energy + 0.15 × 睡了几个小时)
              · rest_until  = 醒来 + 4 小时
            然后 sync_affect 里把 energy 托在这个额度上，按 4 小时线性消退回
            公式的自然值 —— 也就是"早上一睁眼满电，到中午自然回落到日常水平"。
            这样既符合"1 小时回 0.15"，也不会让 energy 全天挂在 1.0 假得很。
            """
            _e0 = clamp(float(s.get("sleep_energy0") or 0.3))
            _recover = 0.15 * h_slept                      # 每小时回 0.15
            s["rest_energy"] = round(min(1.0, _e0 + _recover), 4)
            s["rest_until"] = now_ts + 4 * 3600            # 醒来后 4 小时内有效，之后自然消退
            s["rest_started"] = now_ts
            s["adenosine"] = 0.0 if s["sleep_kind"] != "nap" else clamp(s.get("adenosine", 0.2))
            s["sleep_started_at"] = 0
        return

    today = dt.strftime("%Y-%m-%d")
    # 夜睡：23:00–07:00 且睡眠压力 > 0.50
    if (hour >= 23 or hour < 7) and ade > 0.50:
        s["asleep"] = True
        s["sleep_started_at"] = now_ts
        _pre_sleep_drink(s, now_ts)
        s["sleep_kind"] = "night"
        wake_dt = (dt + timedelta(days=1)).replace(hour=7, minute=30, second=0, microsecond=0)
        jitter = int((random01(s, int(now_ts // 3600)) - 0.5) * 80 * 60)
        s["wake_after"] = wake_dt.timestamp() + jitter
        return
    # 🔴 2026-11-13 加：**实在熬不住了也得睡**，别卡在门槛下面硬扛。
    #   真人不是"腺苷精确到 0.50 才闭眼"：已经很困、时间也很晚了，沾枕头就睡了。
    #   触发条件（任一）：energy 见底 / 褪黑素很高 + 凌晨 / 腺苷已经高过 0.42 又过了一点。
    energy = clamp(s.get("energy", 0.5))
    mel = clamp(s.get("melatonin", 0.0))
    if not asleep and (hour >= 23 or hour < 7):
        collapse = (
            (energy < 0.12 and ade > 0.35)                     # 累垮了
            or (mel > 0.85 and ade > 0.30 and hour >= 0)       # 褪黑素拉满、已经过午夜
            or (ade > 0.45 and hour >= 23)                     # 深夜且已经挺困
        )
        if collapse:
            s["asleep"] = True
            s["sleep_started_at"] = now_ts
            _pre_sleep_drink(s, now_ts)
            s["sleep_kind"] = "night"
            wake_dt = (dt + timedelta(days=1)).replace(hour=7, minute=30, second=0, microsecond=0)
            jitter = int((random01(s, int(now_ts // 3600)) - 0.5) * 80 * 60)
            s["wake_after"] = wake_dt.timestamp() + jitter
            s["collapse_sleep_at"] = now_ts          # 记一笔：这次是困塌了才睡的
            return
    # 午睡：12:00–15:00 且腺苷 > 0.22 且当天没午睡 且每分钟 3% 概率
    if 12 <= hour < 15 and ade > 0.22 and str(s.get("nap_done_day") or "") != today:
        if random01(s, int(now_ts // 60)) < 0.03:
            s["asleep"] = True
            s["sleep_started_at"] = now_ts
            _pre_sleep_drink(s, now_ts)
            s["sleep_kind"] = "nap"
            s["wake_after"] = now_ts + int(20 + 30 * random01(s, int(now_ts // 60) + 7)) * 60
            s["nap_done_day"] = today


def tick(s: dict | None = None, now_ts: float | None = None) -> dict:
    """一次完整结算：drift → sleep → **self_care** → sync → save

    🔴 2026-11-13：`apply_self_care` 必须夹在 drift 之后、sync_affect 之前 ——
       drift 先把饿/渴推上去，self_care 才有东西可判；sync_affect 再据此算情绪。
       顺序反了的话，这一拍的"自己去吃了"不会反映到情绪上，要等下一拍。
    """
    s = s if s is not None else load()
    now_ts = now_ts or time.time()
    drift(s, now_ts)
    maybe_sleep(s, now_ts)
    # ⭐ 没人管的时候自己吃喝（2026-11-13；主人：「没人回也会自己吃喝」）
    try:
        if not s.get("asleep"):          # 睡着了就别吃了
            apply_self_care(s, now_ts)
    except Exception:
        pass                             # 自己吃喝失败不能把心跳搞挂
    sync_affect(s, now_ts)
    # 历史采样（面板近 3 分钟曲线 & 迷你曲线用），最多留 240 点
    hist = s.get("history") or []
    hist.append([int(now_ts), round(clamp(s.get("cortisol", 0.2)), 4), round(clamp(s.get("dopamine", 0.5)), 4),
                 round(clamp(s.get("oxytocin", 0.45)), 4), round(clamp(s.get("melatonin", 0)), 4),
                 round(clamp(s.get("hunger", 0.35)), 4), round(clamp(s.get("thirst", 0.25)), 4),
                 round(clamp(s.get("adenosine", 0.2)), 4), round(float(s.get("core_temp", 36.6)), 3),
                 round(clamp(s.get("cytokines", 0)), 4), round(clamp(s.get("pain", 0)), 4),
                 round(clamp(s.get("estrogen", 0.35)), 4), round(clamp(s.get("progesterone", 0.2)), 4)])
    s["history"] = hist[-240:]
    save(s)
    return s


# ────────────────────────── HTTP ──────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body: bytes, ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = self.path.split("?")[0]
        if p in ("/", "/index.html", "/panel"):
            f = HERE / "panel" / "index.html"
            if f.exists():
                self._send(200, f.read_bytes(), "text/html; charset=utf-8")
            else:
                self._send(404, b"panel missing", "text/plain")
            return
        if p == "/healthz":
            self._send(200, b'{"ok":true}')
            return
        if p == "/api/state":
            self._send(200, json.dumps(load(), ensure_ascii=False).encode())
            return
        if p == "/api/prompt":
            s = load()
            self._send(200, json.dumps({"prompt": prompt_block(s), "target_replies": pick_target(s)},
                                       ensure_ascii=False).encode())
            return
        self._send(404, b'{"error":"not found"}')

    def do_POST(self):
        p = self.path.split("?")[0]
        ln = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(ln) or b"{}")
        except Exception:
            body = {}
        now = time.time()
        if p == "/api/react":
            s = load()
            drift(s, now)
            hit = react(s, str(body.get("text") or ""), now, float(body.get("intensity") or INTENSITY))
            sync_affect(s, now)
            save(s)
            self._send(200, json.dumps({"ok": True, "hit": hit, "state": s}, ensure_ascii=False).encode())
            return
        if p == "/api/eat":
            # ⭐ 2026-11-13 加的（主人：「a吧」）。跟 /api/drink 对称：
            #    force=True ⇒ 不要求文本里有食物词，谁调都算一顿。
            s = load()
            drift(s, now)
            ok = apply_eat(s, str(body.get("text") or "吃饭了"), now,
                           float(body.get("intensity") or INTENSITY), force=True,
                           who=str(body.get("who") or "me"))
            sync_affect(s, now)
            save(s)
            self._send(200, json.dumps({"ok": ok, "hunger": s.get("hunger"),
                                        "last_meal": s.get("last_meal"),
                                        "state": s}, ensure_ascii=False).encode())
            return
        if p == "/api/drink":
            s = load()
            drift(s, now)
            ok = apply_drink(s, "喝水了", now, float(body.get("intensity") or INTENSITY), force=True)
            sync_affect(s, now)
            save(s)
            self._send(200, json.dumps({"ok": ok, "thirst": s.get("thirst"), "state": s}, ensure_ascii=False).encode())
            return
        if p == "/api/tick":
            s = load()
            h = float(body.get("hours") or 0)
            s2 = tick(s, now + h * 3600)
            self._send(200, json.dumps({"ok": True, "state": s2}, ensure_ascii=False).encode())
            return
        self._send(404, b'{"error":"not found"}')


def serve():
    s = tick()          # 启动先结算一次
    # 自己落一份启动日志（从 vbs 隐藏启动时没有控制台）
    try:
        with open(HERE / "vitals.log", "a", encoding="utf-8") as f:
            f.write("%s [vitals] serving http://%s:%d state=%s\n"
                    % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), HOST, PORT, STATE))
    except Exception:
        pass
    """
    🔴🔴 2026-11-17（昨晚实测：日志里 06:42:38 和 06:42:39 连着两行 serving，
      两个进程同时绑 8781，互相抢 state.json 写盘 ⇒ WinError 5「拒绝访问」
      ⇒ tick 算了但写不进 ⇒ 她喝的水被回滚，thirst 一直 1.0）

    根因：`ThreadingHTTPServer` 默认 `allow_reuse_address = 1`，
    Windows 的 SO_REUSEADDR 语义允许**两个进程绑同一端口**。
    ⇒ 关掉它（类属性，必须在实例化之前设），第二个进程会 OSError 退出 ——
      端口本身就是最好的单实例锁。
    """
    class _SingleInstanceServer(ThreadingHTTPServer):
        allow_reuse_address = False
        daemon_threads = True

    try:
        srv = _SingleInstanceServer((HOST, PORT), Handler)
    except OSError as e:
        print("[vitals] 端口 %d 已被占用，本进程退出（单实例保护）：%s" % (PORT, e), flush=True)
        try:
            with open(HERE / "vitals.log", "a", encoding="utf-8") as f:
                f.write("%s [vitals] 端口被占用，退出（已有实例在跑）\n"
                        % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        except Exception:
            pass
        return
    print("[vitals] http://%s:%d  state=%s" % (HOST, PORT, STATE), flush=True)
    srv.serve_forever()


def heartbeat_loop(interval=60.0, first_delay=20.0):
    time.sleep(first_delay)
    while True:
        try:
            tick()
        except Exception as e:
            print("[vitals] tick 失败:", e, flush=True)
        time.sleep(interval)


def selfcheck() -> int:
    ok = True
    # 口渴四档（指南 5.8.2 / 19.3）
    cases = [(0.58, "嗓子有点干"), (0.34, "嘴里有点干"), (0.03, "刚喝过水"), (0.70, "想喝点凉的"), (0.90, "满脑子都是水")]
    for v, want in cases:
        got = thirst_note(v)
        if want not in got:
            print("❌ thirst_note(%.2f) 没有「%s」，实际：%r" % (v, want, got))
            ok = False
    if thirst_note(0.27) != "":
        print("❌ 0.25~0.30 应为中性，实际：%r" % thirst_note(0.27))
        ok = False
    # 喝水识别（19.3）
    for t in ("喝水了", "我去喝水", "递给我一杯水", "喝点水", "喝了一口水"):
        if not is_drink(t):
            print("❌ is_drink(%r) 应为 True" % t)
            ok = False
    if is_drink("今天天气不错"):
        print("❌ is_drink('今天天气不错') 应为 False")
        ok = False
    # 喝水写回 + 冷却
    s = {"thirst": 0.58, "last_drink": 0, "dopamine": 0.5, "endorphin": 0.45,
         "osmolality": 0.48, "blood_volume": 0.77, "angiotensin": 0.23, "fluid_queue": 0}
    if not apply_drink(s, "喝水了", 1000):
        print("❌ apply_drink 应返回 True")
        ok = False
    if abs(s["thirst"] - 0.03) > 1e-9:
        print("❌ 喝水后 thirst 应为 0.03，实际 %.4f" % s["thirst"])
        ok = False
    if any(abs(s[k] - v) > 1e-6 for k, v in (("osmolality", 0.18), ("blood_volume", 0.95),
                                             ("angiotensin", 0.0), ("fluid_queue", 0.55))):
        print("❌ 喝水后其它体液量不对：", {k: s[k] for k in ("osmolality", "blood_volume", "angiotensin", "fluid_queue")})
        ok = False
    if apply_drink(s, "喝水了", 1010) is not False:
        print("❌ 冷却中不应再记一次")
        ok = False
    # 派生量范围
    st = tick(load())
    for k in ("energy", "valence", "irritability", "clinginess", "loneliness"):
        v = st.get(k)
        if v is None:
            print("❌ 缺派生量", k)
            ok = False
    print("✅ 全部断言通过" if ok else "❌ 有断言失败")
    return 0 if ok else 1


def main():
    # Windows 控制台默认 GBK，打印 ✅/❌ 会直接崩 —— 先兜住
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    args = sys.argv[1:]
    if "--selfcheck" in args:
        sys.exit(selfcheck())
    if "--tick" in args:
        i = args.index("--tick")
        h = float(args[i + 1]) if i + 1 < len(args) else 0.1
        s = load()
        s = tick(s, time.time() + h * 3600)
        print(json.dumps({k: s.get(k) for k in ("thirst", "hunger", "energy", "valence", "emotion_level")},
                         ensure_ascii=False))
        return
    if "--serve" in args or not args:
        import threading
        threading.Thread(target=heartbeat_loop, daemon=True).start()
        serve()
        return
    print("用法: --serve | --tick <小时> | --selfcheck")


if __name__ == "__main__":
    main()
