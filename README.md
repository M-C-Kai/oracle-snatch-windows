# oracle-snatch-windows

在 Windows 上 7x24 轮询 [Oracle Cloud Always Free](https://www.oracle.com/cloud/free/) 的
**A1.Flex（ARM）** 实例，赶上放货就自动创建，抢到后写入 `snatched.json` 并退出。
只调用 OCI 官方 API（`oci` Python SDK），不模拟浏览器、不碰控制台。

> 为什么需要它：免费 ARM 额度（最多 4 OCPU / 24 GB）热门区域长期显示
> `Out of host capacity`，但容量会不定期小批量放出（常在半夜）。脚本替你不知疲倦地问。

---

## 一、去 Oracle 控制台拿什么（cloud.oracle.com）

登录 https://cloud.oracle.com ，以下 4 项都在网页控制台里，**不需要发给我/任何人**：

### 1. API 凭据（5 个字段 + 1 个私钥）
`右上角头像 → User Settings → 左侧 Resources → API Keys → Add API Key`
- 选 **Generate API Key Pair**
- 点 **Download Private Key** 保存 `xxx.pem`
- 点 **Add**，弹出 *Configuration File Preview*，复制整段文本

把这段文本存成 **`%USERPROFILE%\.oci\config`**（没有 `.oci` 目录就新建），
把里面的 `key_file=` 改成 `.pem` 的绝对路径，Windows 建议用正斜杠，例如：
```
[DEFAULT]
user=ocid1.user.oc1..aaaa
fingerprint=xx:xx:...
tenancy=ocid1.tenancy.oc1..bbbb
region=ap-tokyo-1
key_file=C:/Users/你/.oci/oracleidentitycloudservice_xxx.pem
```

### 2. 公有子网 OCID（**唯一必填的 OCID**）
`汉堡菜单 → Networking → Virtual Cloud Networks → 你的 VCN → Subnets`
点进一个 **Public Subnet**，复制 **OCID** → 填 `config.env` 的 `SUBNET_ID`。
没有 VCN 就先用控制台向导建一个默认的。

### 3. （可选）确认你的免费额度
`Governance & Administration → Limits, Quotas and Usage`，查 `standard-a1-core-count`。
2026 年起不少账号是 **2 OCPU / 12 GB**（老账号仍是 4/24）。按实际额度改
`config.env` 的 `OCPUS` / `MEMORY_GB`，否则会立刻 `LimitExceeded` 停止。

### 4. 不需要手动拿的
- `COMPARTMENT_ID`：留空 = 根 compartment（tenancy）
- `IMAGE_ID`：留空 = 自动选适配 A1.Flex 的最新 Canonical Ubuntu ARM64

---

## 二、Windows 本机准备

1. 安装 **Python 3.10+**（python.org，勾选 *Add python.exe to PATH*）
2. 生成 SSH 公钥（没有的话）：`ssh-keygen -t ed25519`，得到
   `%USERPROFILE%\.ssh\id_ed25519.pub`
3. 克隆本仓库，建 venv 装依赖：
   ```powershell
   git clone <本仓库URL>
   cd oracle-snatch-windows
   python -m venv .venv
   .\.venv\Scripts\python -m pip install -r requirements.txt
   ```
4. 复制配置模板并填写：
   ```powershell
   copy config.example.env config.env
   notepad config.env
   ```
   至少改：`SUBNET_ID`、`OCPUS`/`MEMORY_GB`（按额度）、确认
   `SSH_PUBLIC_KEY_FILE` 指向你的公钥。`config.env` 已被 `.gitignore` 忽略，
   **不要提交**。

---

## 三、检查运行（务必按顺序）

```powershell
# 1) 本地配置自检：不联网，检查占位符/凭据文件/SSH 公钥
.\.venv\Scripts\python snatch.py --check

# 2) 联机 dry-run：列出可用域 AD、确认镜像，不真开机
.\.venv\Scripts\python snatch.py --dry-run

# 3) 前台试跑（Ctrl+C 可停）
.\start.bat
type snatch.log
```

`--dry-run` 输出类似 `Region ADs: ...` / `Image: Canonical Ubuntu ...` /
`Target: VM.Standard.A1.Flex 4 OCPU / 24 GB` 即为配置正确。

---

## 四、挂成定时任务（常开）

`snatch.py` 自身是 `while True` 死循环，**登录启动一次即可**，不用每分钟触发。

```powershell
powershell -ExecutionPolicy Bypass -File .\install-task.ps1
Start-ScheduledTask -TaskName oracle-snatch
Get-Content .\snatch.log -Tail 30 -Wait
```

脚本做的事：注册一个「当前用户登录时启动」的任务，动作指向 `start.bat`，
`start.bat` 先跑 `--check`，配置没填好就不进主循环（避免空转刷屏）。

> ⚠️ 任务**没有**设「失败时重启」：脚本遇到 `LimitExceeded`（额度已满）会
> 主动退出，这是正确行为，重启只会刷日志。429 限流 / 没货脚本内部已自行
> 退避循环，不会退出。

卸载：`Unregister-ScheduledTask -TaskName oracle-snatch -Confirm:$false`

---

## 五、运行结果

抢到后：
- 终端/日志出现 `SUCCESS in <AD>: instance ocid1.instance...`
- 等实例 `RUNNING` 后取公网 IP，写入 `snatched.json`
- 若配了 Telegram，推一条通知
- 脚本退出（任务不重启）

登录：`ssh -i %USERPROFILE%\.ssh\id_ed25519 ubuntu@<public_ip>`

---

## 六、错误对照

| 日志 | 含义 / 处理 |
|---|---|
| `out of capacity` | 正常，继续等下一轮 |
| `rate limited (429), sleeping 300s` | 请求太密，已自动长退避，别改小间隔 |
| `free-tier limit reached` + 退出 | 免费额度已用完，脚本正确停止，无需重启 |
| `config problem (401/403/404)` | 检查 SUBNET_ID / ~/.oci/config 的 user/tenancy/fingerprint/region |
| `OCI 凭据文件错误` | `%USERPROFILE%\.oci\config` 不存在或 key_file 路径错 |
| `配置检查未通过` | `--check` 没过：占位符没改 / 缺 config / 缺 SSH 公钥 |

---

## 七、注意事项 / 踩坑

- 免费 A1 是**总量**：4核/24GB（或 2/12），可开 1 台 4/24，或拆 2 台 2/12，超了必停。
- 启动盘也算进 200 GB 免费块存储，默认 50 GB 足够，别拉满。
- 别把 `INTERVAL_SECONDS` 改成几秒，只会吃 429，反而更难抢到。
- 账号升级 **PAYG**（按量付费）不会吃掉免费额度，额度内照样 0 元，且体感放货略多；
  务必设 **Budget Alert** 兜底。
- **GitHub Actions 无限定时跑违反 ToS**，别用；老实用自己的 Windows 常开机器。
- `config.env` / `~/.oci/config` / `*.pem` 含敏感信息，仓库已忽略，切勿提交。

## 文件

| 文件 | 作用 |
|---|---|
| `snatch.py` | 主逻辑，单文件 |
| `config.example.env` | 配置模板（复制为 `config.env`） |
| `requirements.txt` | 依赖：`oci>=2.130` |
| `start.bat` | 启动入口，带 `--check` 守卫 + 日志重定向 |
| `install-task.ps1` | 注册/说明 Windows 定时任务 |
| `snatched.json` | 抢到后生成（已忽略） |

## 授权

MIT
