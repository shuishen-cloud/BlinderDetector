"""真实 VLM provider 的解析与容错 —— 不打网络，只测纯函数。

★ 这一层最要紧的性质：**解析失败要退化，不能让链路哑掉**。
  真实模型不一定老实返回 JSON（会加解释、包 ``` 围栏、偶尔少字段）。
"""

import json

from app.contracts import vision_dedup_key
from app.core.providers.openai_compat import parse_reply
from app.core.rules import scene


def _reply(**kw):
    base = {
        "scene_description": "前方约 3 米有一个红绿灯，现在是红灯",
        "scene_conf": 0.9,
        "objects": [{"label": "红绿灯", "position": "center",
                     "bbox": [0.44, 0.18, 0.06, 0.14], "distance_m": 3.0,
                     "confidence": 0.88}],
        "ocr_results": [],
    }
    base.update(kw)
    return json.dumps(base, ensure_ascii=False)


# --------------------------------------------------------------------------
# 正常解析
# --------------------------------------------------------------------------


def test_parses_plain_json():
    d = parse_reply(_reply())
    assert d["scene_description"].startswith("前方约 3 米")
    assert d["scene_conf"] == 0.9
    assert d["objects"][0]["label"] == "红绿灯"
    assert d["objects"][0]["bbox"] == [0.44, 0.18, 0.06, 0.14]


def test_parses_json_inside_a_code_fence():
    """模型很爱包 ```json 围栏"""
    d = parse_reply(f"```json\n{_reply()}\n```")
    assert d["objects"][0]["label"] == "红绿灯"


def test_parses_json_surrounded_by_chatter():
    d = parse_reply(f"好的，这是结果：\n{_reply()}\n希望有帮助。")
    assert d["objects"][0]["label"] == "红绿灯"


def test_keeps_ocr_results():
    d = parse_reply(_reply(ocr_results=[
        {"text": "电梯", "category": "elevator_button",
         "bbox": [0.62, 0.31, 0.11, 0.08], "confidence": 0.93},
    ]))
    assert d["ocr_results"][0]["text"] == "电梯"


# --------------------------------------------------------------------------
# ★ 容错：解析不出来要退化，不能返回空
# --------------------------------------------------------------------------


def test_plain_text_falls_back_to_scene_description():
    """★ 不是 JSON -> 整段当描述。

    宁可去重粒度变粗（播得啰嗦），也不能返回空 ——
    空 = 用户听不到声音 = 他会以为环境安全。
    """
    d = parse_reply("前方有个红绿灯，现在是红灯")
    assert d["scene_description"] == "前方有个红绿灯，现在是红灯"
    assert d["objects"] == []


def test_empty_reply_returns_empty_dict():
    assert parse_reply("") == {}
    assert parse_reply("   ") == {}


def test_json_without_description_falls_back():
    d = parse_reply('{"scene_conf": 0.5}')
    assert d["scene_description"], "缺 scene_description 时该退回原文"


def test_broken_json_falls_back():
    d = parse_reply('{"scene_description": "前面有台阶", ')  # 截断
    assert d["scene_description"]


# --------------------------------------------------------------------------
# 清洗：模型偶尔给错类型
# --------------------------------------------------------------------------


def test_objects_without_label_are_dropped():
    d = parse_reply(_reply(objects=[
        {"position": "center"},                      # 没 label -> 丢
        {"label": "台阶", "confidence": 0.9},
    ]))
    assert [o["label"] for o in d["objects"]] == ["台阶"]


def test_confidence_is_clamped_and_coerced():
    d = parse_reply(_reply(scene_conf=1.7, objects=[
        {"label": "台阶", "confidence": "0.9"},      # 字符串
        {"label": "车", "confidence": -3},           # 越界
    ]))
    assert d["scene_conf"] == 1.0
    assert d["objects"][0]["confidence"] == 0.9
    assert d["objects"][1]["confidence"] == 0.0


def test_bad_position_defaults_to_center():
    d = parse_reply(_reply(objects=[{"label": "台阶", "position": "up-left"}]))
    assert d["objects"][0]["position"] == "center"


def test_objects_not_a_list_is_tolerated():
    d = parse_reply(_reply(objects="前面有台阶"))
    assert d["objects"] == []


# --------------------------------------------------------------------------
# ★ 回归：结构化输出必须真的救回「去重塌缩」
# --------------------------------------------------------------------------


def test_different_scenes_get_different_dedup_keys():
    """★ 这是引入 JSON prompt 的**唯一理由**。

    只让模型说一句话时，objects/ocr 恒空 -> scene_key 恒为 default ->
    三种场景的 dedup_key 全是 vision:scene:default，第一层会反复播同一句、
    场景变了也不播。有了 objects 才有区分度。
    """
    keys = set()
    for desc, label in (
        ("前方约 3 米有一个红绿灯，现在是红灯", "红绿灯"),
        ("台阶，注意脚下", "台阶"),
        ("左侧一米左右有人经过", "人"),
    ):
        d = scene.apply(parse_reply(_reply(scene_description=desc, objects=[
            {"label": label, "position": "center", "confidence": 0.9},
        ])))
        keys.add(vision_dedup_key(d))

    assert len(keys) == 3, f"三种场景应拿到三个不同的去重键，实际 {keys}"
    assert "vision:scene:default" not in keys


def test_prose_only_reply_collapses_to_one_key():
    """对照：纯文本回复（旧行为）确实会塌成一个键 —— 留着当反例。

    注意塌成的具体值不必是 `default`：没有 objects/ocr 时 `classify([], [])`
    返回 `clear`，也就是**「描述的是红绿灯」和「前方真的空旷」拿到同一个键**，
    比 default 更糟。所以这里断言的是「无法区分」，不是某个具体值。
    """
    keys = set()
    for desc in ("前方有个红绿灯", "台阶注意脚下", "有人经过"):
        d = scene.apply(parse_reply(desc))  # 非 JSON -> 退化成纯文本
        keys.add(vision_dedup_key(d))
    assert len(keys) == 1, f"纯文本本来就区分不了场景，应塌成一个键，实际 {keys}"
