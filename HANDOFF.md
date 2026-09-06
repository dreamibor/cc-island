# HANDOFF — cc-island Windows 移植（adapt-win 分支）工作交接

> 更新日期：2026-09-07 · 分支：`adapt-win` · HEAD：`a5345a0`
> 设备：M5Stack StopWatch（ESP32-S3，COM3）· 当前固件已烧录并运行 · bridge 正在后台连接推送

---

## 1. 当前状态（TL;DR）

- **手表**：已烧录 adapt-win 固件，CC Island app 正常运行，蓝牙地址固定为
  `D8:74:88:86:95:03`（NVS 持久化，重启/双方重启均不变）。
- **Windows bridge**：正在后台运行（`--ble 5`），已验证向手表成功推送 GLM/DeepSeek/ChatGPT 数据。
- **数据面**：GLM（lite 套餐，5h/每周窗口）✅、DeepSeek（¥188.82）✅、ChatGPT ✅、
  Claude ❌（本机未执行 `claude /login`，表盘该行为占位 `--`）。
- **key 存放**：`C:\Users\ZHAOYU\.cc-island\config.json`（专用配置文件，模板已建好，填 key 即用）。
- **工具链**：本机 ESP-IDF **v6.1**（`C:\esp\v6.1`）可用；上游 README 写的 v5.5.4 **不需要**，
  M5GFX 已在集成脚本中升级至上游 master（0.2.28，官方 v6 适配）。

## 2. 分支提交索引（adapt-win）

| 提交 | 内容 |
|---|---|
| `8f111ba` | Windows 移植主体：bridge 跨平台化（keychain→文件、GLM/DeepSeek 数据面、payload v3）、部署脚本、固件两页 UI、设计文档 |
| `2a4eaec` + `b5f72ac` | GLM/DeepSeek 官方图标（clover 之前的鲸鱼/Z 版本） |
| `d7109dd` | 字段精简（窗口行 `{h,d,r}`、DS 行 `{cur,bal}`）、Codex→ChatGPT 更名、官方 logo、IDF v6.1 编译修复、DeepSeek 行状态条 |
| `0eb6b02` | 行内图标/文字中轴对齐、四枚 logo 统一取景（42px/居中） |
| `705e0c0` | **蓝牙广播链路修复**（广播包超长、NimBLE 栈溢出、随机地址时序）+ GLM 毫秒重置时间修正 + 硬件实测通过 |
| `12fd431` | DeepSeek 行重做：右上 `--` 占位、状态条满刻度 200 CNY、灰色行显示具体余额 |
| `a5345a0` | BLE 地址改为 **NVS 持久化**（重启稳定）+ 连接加固 + 文档/图标/串口脚本 |
| `7b82aa2` | 重渲染 SVG 源（去除浏览器滚动条污染） |

设计文档（架构、payload、发现顺序、排查手册）：`docs/windows-port-design.zh-CN.md`（§0 为最新修订记录）。

## 3. 系统架构速览

```
手表（ESP32-S3, 固件 adapt-win）          Windows bridge（python, IDF venv 可用）
 CC Island app：                          bridge/codexisland_bridge.py
  · 两页四行 UI（触摸翻页，蓝键刷新）        · 读 ~/.codex/auth.json (ChatGPT)
  · NVS 固定地址 D8:74:88:86:95:03          · 读 ~/.cc-island/config.json (GLM/DS key)
  · NUS: 6e400001/0002(write)/0003(notify)  · GLM: bigmodel.cn/api/monitor/usage/quota/limit
                                           · DeepSeek: api.deepseek.com/user/balance
                                           · Claude: keychain/.credentials.json OAuth
 payload v3:                               · 每 5 分钟 + 蓝键触发推送
 {"c":{h,d,r},"x":{h,d,r},"g":{h,d,r},     · 断线自动重连（扫描按名称匹配）
  "ds":{cur,bal}}
```

表盘布局：第 1 页 Claude/ChatGPT 窗口行；第 2 页 GLM 窗口行 + DeepSeek 余额行
（状态条满刻度 200 CNY，灰色行显示具体余额，右上 `--` 占位）。

## 4. 环境与常用命令

```powershell
# ESP-IDF 环境（每次新终端）— 注意：从 Git Bash 启动需先清 MSYSTEM 环境变量
Remove-Item Env:MSYSTEM -ErrorAction SilentlyContinue
. 'C:\Espressif\tools\Microsoft.v6.1.PowerShell_profile.ps1'
cd C:\Users\ZHAOYU\Desktop\Develop\Stopwatch\cc-island\build-firmware
idf.py build                      # 编译
idf.py -p COM3 flash              # 烧录（设备 = COM3）
idf.py -p COM3 monitor            # 串口日志（ble_nus 的 identity/adv/connect 日志在这看）

# 改了 CMakeLists 或资产文件列表后，必须先 reconfigure（ninja 构建中重生成有
# build_properties 序列化 bug，见 §6）：
idf.py reconfigure && idf.py build

# Bridge（Windows Python = IDF venv，已装 bleak 3.0.2）
cd C:\Users\ZHAOYU\Desktop\Develop\Stopwatch\cc-island
& C:\Espressif\tools\python\v6.1\venv\Scripts\python.exe bridge\codexisland_bridge.py --ble 5
# 诊断数据面（不连蓝牙）：
& C:\Espressif\tools\python\v6.1\venv\Scripts\python.exe bridge\codexisland_bridge.py --json

# 串口抓取（BLE 调试利器，scripts/ble_serial_capture.py，需 pyserial+esptool）
python scripts\ble_serial_capture.py COM3 180   # 复位芯片并抓 180s 日志
```

注意：本机**没有**独立的 python.org Python；`python` 命令是 Microsoft Store 占位符。
Windows 侧一切 Python 用 IDF venv；WSL 内有 Ubuntu 24.04 + Python 3.12（数据面测试可用，
**蓝牙不行**——WSL 无蓝牙直通）。

## 5. 关键决策与踩坑记录（含证据）

### 固件 / BLE（全部有串口日志实证）

| 坑 | 现象 | 修复 |
|---|---|---|
| 广播包 32 字节超 31 上限 | 设备无名无 UUID，扫描不到 | 名称留广播包、128-bit UUID 移入 scan response（`ble_gap_adv_rsp_set_fields`） |
| NimBLE host task 栈溢出（4096） | 广播启动即崩溃重启（串口可见 stack overflow backtrace） | sdkconfig 提升至 8192（install_firmware.sh 已记录） |
| GLM nextResetTime 是**毫秒** | 表盘显示 298 亿分钟倒计时 | `_parse_reset`/`_reset_min` 归一化（>10¹² 视为 ms） |
| 随机地址在 host 同步前设置 | `adv_start rc=21 (ENOADDR)`，identity 全零 | 移入 `on_sync` 回调（HCI 通道同步后才可用） |
| 随机地址 MSB 位设错字节 | `set_rnd` 校验拒绝（要求 val[5] 高 2 位=11） | `rnd[5] |= 0xC0` |
| 配对密钥不持久 → 手表关机重启后，Windows 已配对记录重连失败，状态在"已连接/已配对"间切换，永远建不成工作连接（删除设备重配可临时恢复） | `CONFIG_BT_NIMBLE_NVS_PERSIST=y` + `ble_store_config_init()`：配对密钥与 CCCD 状态持久化到 NVS，重启后密钥仍在，Windows 自动重连成功 |

### BLE 地址策略（重要设计决策）

地址 = **NVS 持久化的随机静态地址**（namespace `cc_island`，key `ble_addr`）：

- 手表重启、Windows 重启 → 地址不变，Windows 复用缓存的 GATT 库，秒连。
- **Windows 按“地址 + GATT 库哈希”缓存设备**：固件迭代导致 GATT 布局变化后，
  Windows 会对旧地址进入死循环（自动连接 → 立即 0x13 终止，实测 4 分钟 220 次）。
  此时 **bump `ble_nus.h` 里的 `kGattDbVersion`** → 地址轮换 → Windows 视为新设备，立刻恢复。
- 临时中毒（非固件变更）的现场解法：管理员 PowerShell `Restart-Service bthserv -Force`
  （本次实测有效；脚本模板在 `%TEMP%\ccisland_selftest\reset_bt_stack.ps1`）。
- 开发期避免**强杀 python 进程**结束 bridge——会留下幽灵 GATT 会话占住连接。
- 出厂设备初见时手动“添加设备”过的记录也会占住连接，同样用 bthserv 重启清掉。

### Windows / 工具链

| 坑 | 处理 |
|---|---|
| Git Bash 的 CRLF + heredoc 会破坏 bash 脚本与 python stdin | 已加 `.gitattributes`（`*.sh eol=lf`）；复杂脚本一律走 Write 工具落盘再执行 |
| Git Bash → PowerShell 会带入 MSYSTEM，ESP-IDF python 拒绝 | 启动前 `Remove-Item Env:MSYSTEM`（构建链已内置） |
| Git Bash 传 `/mnt/c/...` 参数会被 MSYS 路径转换改写 | 前缀 `MSYS_NO_PATHCONV=1` |
| PowerShell 5.1 调 WinRT 需全限定类型加载 + 枚举参数 | 见 `%TEMP%\ccisland_selftest\toggle_bt.ps1`（临时） |
| bleak 3.0.2：Windows 对刚重启设备的 GATT 发现偶发不完整 | `connect_watch` 已加 NUS 服务校验，失败快速重试 |
| 强杀 bridge 进程会留下幽灵 GATT 会话占住手表 | 停 bridge 用 Ctrl-C 正常退出；已中毒则重启 bthserv |

## 6. 已知问题 / 待办

1. **Claude 行**：本机未登录 Claude → 占位。执行 `claude /login` 后自动亮起。
2. **ChatGPT 行**：测试时 token 曾过期（`codex login` 可续）；当前已有真实数据。
3. **GLM 端点假设**：5h/weekly 顺序、毫秒时间戳已实测校准 ✓；端点本身仍属社区事实，
   失效时用 `--glm-endpoint` 切换（z.ai 国际版自动识别）。
4. **bridge 常驻**：尚未注册自启。需要时以管理员运行
   `powershell -File scripts\setup_autostart.ps1`（任务计划 `CCIslandBridge`）。
5. **GATT 布局将来变更时**：bump `ble_nus.h` 的 `kGattDbVersion`（地址自动轮换，避开 Windows 缓存）。
6. **配对密钥已持久化**（2026-09-07 修复）：Windows 侧配对一次后，手表关机/重启都能自动重连。
   若将来 Windows 又出现"连接/已配对"反复切换：先在设置里删除设备并重配一次；若固件改了 GATT 布局，
   bump `kGattDbVersion`。深度排查工具：`scripts/ble_serial_capture.py`（串口抓取，本次 BLE 调试的主力工具）；
   临时目录的 `reset_bt_stack.ps1` / `toggle_bt.ps1` 若需要可移入 scripts/。

## 7. 验证记录（2026-09-07）

- bridge 推送实测（手表收到）：`{"c":{...},"x":{"h":1,"d":12,"r":134},
  "g":{"h":2,"d":0,"r":57},"ds":{"cur":"CNY","bal":188.82}}`
- GLM 数据面：lite 套餐 / 5h 2% / 每周 0% / 重置 97min（毫秒归一化后）✓
- DeepSeek 数据面：CNY 188.82 ✓
- 连接事件（设备串口）：`connect status=0` + CCCD subscribe ✓
- 重启持久化：复位重启后 identity 仍为 `d8:74:88:86:95:03` ✓
- bridge 自测：key 发现顺序 / compact v3 / 渲染 / 毫秒归一化全过
