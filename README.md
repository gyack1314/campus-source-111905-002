# 学习活动合成记录库

此仓库提供本地授权演练用来源服务、协议样本、来源自身的测试，以及一个收集客户端 `collector`。全部机构、课程、账号、作品及观测均为合成资料。使用前阅读 [来源契约](docs/来源契约.md) 与 [收集器说明](docs/收集器说明.md)。

Python 3.11及以上。安装固定依赖后，运行：

    python -m pip install -r requirements.txt
    python -m source.server --port 8765

服务默认绑定本机环境的8765端口，健康检查地址为http://127.0.0.1:8765/health。浏览器门户入口为契约所列资料端点。服务重启会重置演练状态。

## 收集客户端

收集器把一条活动、它引用的原始材料与后来针对它的作废语句一起追溯留存，并提供不包含已作废活动的有效参与视图。先启动来源服务，然后：

    python -m collector.cli collect  --base http://127.0.0.1:8765 --repo ./evidencerepo
    python -m collector.cli status   --repo ./evidencerepo
    python -m collector.cli evidence --repo ./evidencerepo
    python -m collector.cli show <statement-uuid> --repo ./evidencerepo

`collect` 可反复运行：它以本地确认过的 `X-Experience-API-Consistent-Through` 为
stored 下界接续读取，中断不推进水位，晚到的旧活动入库后自动进入。`status` 显式
给出每条语句的当前有效性、材料完整性、未闭合引用与材料缺口，不以最终条数掩盖漏
项。授权令牌通过 `--token` 或环境变量 `LRS_TOKEN` 提供，只用于请求头，不落盘。

## 测试

来源与收集器测试：

    python -m unittest discover -s tests -v

独立容器可用docker build -t local-source .构建，docker run --rm -p 8765:8765 local-source启动。来源测试也可在构建镜像中运行：docker run --rm --network none local-source python -m unittest discover -s tests -v。容器使用非root用户，测试仅连接同容器回环源；笔记本WebSocket也在同容器内连接。

测试只检查来源如何发布资料与响应页面操作，以及收集器如何留存与解释这些资料；不能替代真实环境的合规评估。控制接口仅供本地演练。文档与代码没有真实服务凭据。
