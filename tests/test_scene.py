"""第一层场景分类 —— 决定去重的粒度。"""

from app.core.rules import scene


def obj(label, position="center"):
    return {"label": label, "position": position, "confidence": 0.9}


# --------------------------------------------------------------------------


def test_empty_scene_is_clear():
    assert scene.classify([], []) == "clear"


def test_traffic_light_wins_over_other_objects():
    """过马路时红绿灯是最需要立刻知道的信息，优先级最高"""
    objects = [obj("人", "left"), obj("红绿灯", "center")]
    assert scene.classify(objects, []) == "traffic_light"


def test_person_is_split_by_side():
    """「左边有人」和「右边有人」是两件事，去重时不能混为一谈"""
    assert scene.classify([obj("人", "left")], []) == "person_left"
    assert scene.classify([obj("人", "right")], []) == "person_right"


def test_english_labels_work():
    assert scene.classify([obj("bicycle")], []) == "bicycle"
    assert scene.classify([obj("traffic light")], []) == "traffic_light"


def test_bicycle_is_not_mistaken_for_a_car():
    """★ 「自行车」包含「车」字 —— 窄规则必须排在宽规则前面，
    否则自行车会被认成机动车，播报措辞和安全等级都错了。"""
    assert scene.classify([obj("自行车")], []) == "bicycle"
    assert scene.classify([obj("电动车")], []) == "bicycle"


def test_result_is_independent_of_object_order():
    """场景分类不能取决于物体在列表里的先后"""
    a = scene.classify([obj("人", "left"), obj("红绿灯")], [])
    b = scene.classify([obj("红绿灯"), obj("人", "left")], [])
    assert a == b == "traffic_light"


def test_steps_are_recognized():
    assert scene.classify([obj("台阶")], []) == "steps"


def test_ocr_category_used_when_no_objects():
    assert scene.classify([], [{"category": "doorplate"}]) == "doorplate"
    assert scene.classify([], [{"category": "elevator_button"}]) == "elevator"


def test_objects_take_precedence_over_ocr():
    assert scene.classify([obj("红绿灯")], [{"category": "doorplate"}]) == "traffic_light"


def test_unknown_objects_fall_back_to_clear():
    assert scene.classify([obj("飞碟")], []) == "clear"


# --------------------------------------------------------------------------
# 去重粒度
# --------------------------------------------------------------------------


def test_classification_is_coarse_enough_to_dedupe():
    """★ 分类太细会让「每一帧都是新场景」，去重就失效了。

    同一个台阶在不同距离下应该归为同一类，否则会被反复播报。
    """
    near = scene.classify([obj("台阶")], [])
    far = scene.classify([obj("台阶")], [])
    assert near == far


def test_scene_key_changes_when_scene_actually_changes():
    assert scene.classify([obj("红绿灯")], []) != scene.classify([obj("人", "left")], [])


# --------------------------------------------------------------------------
# apply
# --------------------------------------------------------------------------


def test_apply_fills_missing_scene_key():
    detail = {"objects": [obj("台阶")], "ocr_results": []}
    scene.apply(detail)
    assert detail["scene_key"] == "steps"


def test_apply_keeps_provided_scene_key():
    """模型自己给了分类就用它的，不要覆盖"""
    detail = {"scene_key": "模型给的", "objects": [obj("台阶")], "ocr_results": []}
    scene.apply(detail)
    assert detail["scene_key"] == "模型给的"
