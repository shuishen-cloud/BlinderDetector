"""HTTP / WebSocket 层。

分界线：`app/core/` 放业务逻辑（各层编排、规则、provider），这里只放
**和传输方式绑在一起**的东西 —— 收 multipart 的、发 JSON 的、连 WS 的。

拆开的理由：原先这些全挤在 `main.py` 的一个 `create_app()` 里（370 行），
装配和实现混在一起。想知道「`/v1/frame` 怎么校验」得先翻过整张路由表，
想知道「路由表长什么样」又得先翻过 200 行 handler。现在 `main.py` 只负责装配。
"""
