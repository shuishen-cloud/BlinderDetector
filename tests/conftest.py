"""全局测试夹具。

★ 把路线数据源**钉死在 builtin**，让测试既不依赖开发机的 `.env`、也不依赖网络。

  开发机常常配着 `ROUTER=baidu`（要看调试台的真实路线，见 `.env` 的说明）。
  那样一来，`POST /v1/navigation/route` 的用例就会**真的去打百度接口**：

  · 结果取决于网络通不通、配额还剩多少 —— 测试变成了环境探针；
  · 每次跑测试都在烧使用者的配额；
  · 而且它**照样会绿**（失败时图层会如实降级到内置路网），所以没人会发现
    自己测其实不是真实地图那条路 —— 一个很安静的假绿。

  真实地图自己的行为由 `test_baidu_router.py` 覆盖，那边直接构造
  `BaiduRouter`，不发网络请求。

★ `BAIDU_AK` 故意**不动**：`test_frontend_config_never_leaks_the_server_side_ak`
  靠它判断「本机有没有配服务端 AK」，配着才有意义。

★ 感知层与检测器同理，而且这条**不是预防性的** —— 2026-09-23 接上真实
  VLM（`.env` 里 `VLM_PROVIDER=dashscope` + 真 key）之后，`test_api.py` 里
  所有走 `perception` 的用例**真的去打 DashScope 了**：10 条红，而且是
  HTTP 400（上传的是测试用的小图，接口不收）。它们要是碰上能收的图，
  就是在烧使用者的额度 —— 和 ROUTER 那条一模一样的环境探针问题。
  测试必须只依赖仓库里的东西，不依赖开发机上配了什么。
"""

from __future__ import annotations

import pytest

from app import config


@pytest.fixture(autouse=True)
def _offline_router(monkeypatch):
    """所有用例都在内置路网下跑 —— 离线、确定、不烧配额。"""
    monkeypatch.setattr(config, "ROUTER", "builtin")
    monkeypatch.setattr(config, "BAIDU_FIXTURE", "")


@pytest.fixture(autouse=True)
def _offline_vlm(monkeypatch):
    """感知层也钉在 mock 上 —— 理由同 ROUTER，但这条是真踩过的。

    ★ key 一并清空，是**故意的**：这样即便哪天有代码绕过 provider 选择、
      直接去实例化 OpenAI 兼容实现，它也会当场抛
      「需要 VLM_API_KEY」而不是安静地把请求发出去。
      测试里的失败要么看得见，要么不发生。
    """
    monkeypatch.setattr(config, "VLM_PROVIDER", "mock")
    monkeypatch.setattr(config, "DETECTOR", "mock")
    monkeypatch.setattr(config, "VLM_API_KEY", "")
    monkeypatch.setattr(config, "VLM_BASE_URL", "")
