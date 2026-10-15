# MockServer — 轻量级本地挡板工具

单文件、零依赖的本地 Mock/挡板服务:一个 `mock_server.py` + Python 3.8+,开箱即用;Windows 用户也可以直接下载打包好的 **mock_server.exe**,无需安装任何环境。
自带网页管理界面,可视化配置接口的匹配条件与响应内容,联调、测试、前端并行开发都不用等后端接口 ready。

## 特性

- **零依赖**:仅使用 Python 标准库,`pip install` 都不需要
- **单文件**:整个服务就一个 `mock_server.py`,拷到哪都能跑;也提供免安装 exe
- **可视化管理界面**:内置网页控制台,增删改查规则、查看请求日志
- **多维匹配**:请求方法 / URL 路径(支持 `*` 通配) / 查询参数 / 请求头 / 请求体关键字,条件留空即不校验
- **灵活响应**:状态码 / 响应头 / 响应体 / 延迟返回(模拟慢接口),全部可配
- **文件返回**:规则可指定本地文件路径,原样返回二进制内容——文件/图片下载类接口的挡板一步到位
- **规则自测**:每条规则可点「测试」模拟一次请求,预览是否命中、会不会被其他规则抢先,未保存的草稿也能测
- **HTTPS 支持**:可选开启,自带自签名证书方案,与 HTTP 共用同一套规则
- **规则持久化**:自动保存到同目录 `rules.json`,重启不丢

## 快速开始

### 方式一:exe 直接使用(Windows,推荐)

从 [Releases](../../releases) 下载 `mock_server.exe`,放到任意目录:

- **双击运行**:默认端口 8899,控制台会打印管理界面地址,关闭窗口即停止服务
- **命令行运行**(可带端口参数):

```bat
mock_server.exe          :: 默认端口 8899
mock_server.exe 9090     :: 自定义端口
```

`rules.json`、HTTPS 证书等运行时文件都自动创建在 **exe 所在目录**,换目录互不影响。

### 方式二:Python 直接运行(全平台)

```bash
python mock_server.py            # 默认 HTTP 端口 8899
python mock_server.py 9000        # 自定义 HTTP 端口
python mock_server.py 9000 9443   # 同时开启 HTTPS (需证书, 见下文)
```

启动后浏览器打开 **http://127.0.0.1:8899/** 即可进入管理界面。

> Windows 注意:若 `python3` 指向 Microsoft Store 的应用别名,执行会静默退出,请使用 `python` 或 `py` 命令。

## 管理界面

| 功能 | 说明 |
|---|---|
| 接口规则 | 规则自上而下匹配,命中第一条即返回;可启用/停用、排序、编辑、删除 |
| 请求日志 | 最近 200 条请求的时间、方法、路径、命中的规则、状态码 |
| 规则测试 | 按规则自身匹配条件构造模拟请求(不发真实请求),预览命中结果与实际返回 |

## 规则字段说明

| 字段 | 说明 |
|---|---|
| `method` | 请求方法,`ANY` 表示不限 |
| `path` | URL 路径,不含查询参数;支持 `*` 通配(如 `/api/*`) |
| `req_query` | 查询参数匹配(JSON);值为空串表示只要求该参数存在 |
| `req_headers` | 请求头匹配(JSON),需完全一致才命中 |
| `req_body_contains` | 请求体包含该文本才命中 |
| `status` | 响应状态码 (100–599) |
| `resp_headers` | 响应头(JSON) |
| `resp_body` | 响应体(文本) |
| `resp_file` | 响应文件路径(可选,优先于响应体),原样返回文件二进制内容;相对路径以 `mock_server.py` 所在目录为基准 |
| `delay_ms` | 延迟返回毫秒数(0–60000),用于模拟慢接口 |

完整示例见 [rules.example.json](rules.example.json),可直接改名成 `rules.json` 使用。

## 文件下载类挡板

把要返回的文件(如 `abc123.png`)放在 `mock_server.py` 同目录,规则这样配:

- 路径:`/api/download`,查询参数匹配:`{"fileId": "abc123"}`
- 响应头:`{"Content-Type": "image/png"}`
- 响应文件路径:`abc123.png`

请求端即可正常下载:

```bash
curl -o photo.png "http://127.0.0.1:8899/api/download?fileId=abc123"
```

`fileId` 参数不同、返回文件不同——用查询参数匹配做多条规则即可。

## 开启 HTTPS

```bash
python mock_server.py 8899 8443
```

需要同目录存在自签名证书 `mock_cert.pem` / `mock_key.pem`,生成方式(任选其一):

```bash
# 方式一: openssl
openssl req -x509 -newkey rsa:2048 -keyout mock_key.pem -out mock_cert.pem \
  -days 3650 -nodes -subj "/CN=localhost" \
  -addext "subjectAltName=DNS:localhost,IP:127.0.0.1"

# 方式二: Python cryptography 库生成(略, 参考 openssl 方式即可)
```

启动后:

- HTTP 与 HTTPS 共用同一套规则
- 管理界面与接口均可通过 `https://127.0.0.1:8443/` 访问
- 自签名证书首次访问会有浏览器警告,点「高级 → 继续前往」即可;`curl` 加 `-k` 跳过校验

> 证书与私钥属于本地运行时文件,已在 `.gitignore` 中排除,不要提交到仓库。

## 常见问题

**Q: 运行后直接就退出了,没有任何输出?**
Windows 上 `python3` 是 Microsoft Store 的应用别名占位程序。用 `python mock_server.py` 或 `py mock_server.py`。可在「设置 → 应用 → 高级应用设置 → 应用执行别名」关闭该占位符。

**Q: 端口被占用?**
换一个端口启动:`python mock_server.py 9090`。

**Q: 配了规则但请求 404?**
规则自上而下匹配,可能被更靠上的规则抢先命中。用规则列表的「测试」按钮验证,弹窗会告诉你实际命中的是哪条规则。

**Q: 控制台出现 `ConnectionResetError` / `WinError 10054`?**
客户端在请求前就断开了连接(常见于浏览器预建连接),属正常网络噪声,服务已静默处理,不影响功能。

**Q: 下载的 exe 被杀毒软件/Windows Defender 误报?**
PyInstaller 单文件 exe 是常见误报对象。本项目代码全部开源([mock_server.py](mock_server.py) 单文件、纯标准库),可自行审查后白名单,或改用 `python mock_server.py` 从源码运行。

## 从源码构建 exe

发布用的 `mock_server.exe` 由 PyInstaller 打包,全平台构建命令(在 Windows 上执行):

```bash
pip install pyinstaller
python -m PyInstaller --onefile --clean --name mock_server mock_server.py
```

产物在 `dist/mock_server.exe`,单文件约 8.5MB,用户免装 Python 直接运行。构建产物(`build/`、`dist/`、`*.spec`)不入库,exe 通过 GitHub Releases 分发。

## 目录说明

```
mock_server.py        # 全部代码, 单文件
rules.json            # 运行时自动生成/保存的规则(不提交)
rules.example.json    # 规则示例
mock_cert.pem         # HTTPS 自签名证书(自行生成, 不提交)
mock_key.pem          # HTTPS 私钥(不提交)
build/  dist/         # PyInstaller 构建产物(不提交)
```

## License

[MIT](LICENSE)
