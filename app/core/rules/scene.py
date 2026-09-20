"""
第一层：场景分类。

用途是**决定去重的粒度**。没有它，红绿灯持续 40 秒会被播 8 遍；
有了它，同一个场景只播一次，场景变了才播。

分类刻意做得粗：分得太细会让「每一帧都是新场景」，去重就失效了，
等于没做。宁可粗一点。
"""

from __future__ import annotations

#: 物体标签 -> 场景分类。
#:
#: ★ 顺序即优先级：先拿**所有物体**去匹配第一条规则，全都不中才看第二条。
#:   不能按物体遍历 —— 否则「人 + 红绿灯」会取决于哪个物体排在前面。
#:
#: ★ 窄规则必须排在宽规则前面：`自行车` 要在 `车` 之前，因为
#:   `"车" in "自行车"` 为真，反过来「自行车」会被认成「机动车」。
_LABEL_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("红绿灯", "信号灯", "traffic light"), "traffic_light"),
    (("自行车", "单车", "bike", "bicycle", "电动车"), "bicycle"),
    (("车", "汽车", "轿车", "car", "vehicle", "卡车", "公交"), "vehicle"),
    (("台阶", "楼梯", "step", "stair"), "steps"),
    (("电梯", "elevator"), "elevator"),
    (("门", "door", "玻璃"), "door"),
    (("人", "行人", "person", "people"), "person"),
    (("柱子", "电线杆", "pole", "树", "tree"), "static"),
)

#: OCR 文本类别 -> 场景分类
_OCR_RULES: tuple[tuple[str, str], ...] = (
    ("elevator_button", "elevator"),
    ("doorplate", "doorplate"),
    ("menu", "menu"),
    ("sign", "sign"),
)


def classify(objects: list[dict], ocr_results: list[dict]) -> str:
    """从物体和 OCR 结果推导场景分类。

    返回的 key 会进去重键，所以它变化就意味着「值得再播一次」。

    规则由高优先级到低优先级逐条匹配全部物体 —— 不是逐个物体匹配规则。
    这样结果与物体的排列顺序无关。
    """
    for keys, scene in _LABEL_RULES:
        for o in objects or []:
            label = str(o.get("label", "")).lower()
            if any(k in label for k in keys):
                # 人单独区分左右 —— 「左边有人」和「右边有人」是两件事
                if scene == "person":
                    return f"person_{o.get('position', 'center')}"
                return scene

    for r in ocr_results or []:
        cat = r.get("category", "unknown")
        for key, scene in _OCR_RULES:
            if cat == key:
                return scene

    return "clear"


def apply(detail: dict) -> dict:
    """补上 detail 里缺的 scene_key。VLM 自己给了就用它的。"""
    if not detail.get("scene_key"):
        detail["scene_key"] = classify(
            detail.get("objects", []),
            detail.get("ocr_results", []),
        )
    return detail
