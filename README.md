# codex-voice-orb

按住说话，把一句话交给你 Mac 上的 Codex CLI，再把回复里的短结论读出来。形态上接近 OPPO 的「心力球」：领夹麦负责收音，智能体在另一头做事。这里的智能体是本机的 `codex`，硬件是现成的无线领夹麦和 Mac 的扬声器或耳机。

这不是实时闲聊音箱。Codex 要思考、读仓库、跑命令，一轮经常要几秒到几十秒。说完会先听到一声「好。」，完整回复打在终端里，语音只读一句摘要。

## 一轮对话

1. 按住热键说话（默认 `Ctrl+Alt+Space`，避开中文输入法的 Ctrl+Space），或让 VAD 在你停顿后收尾。单段最长默认 30 秒。
2. 本机 faster-whisper（或 whisper.cpp）转成中文或英文。
3. 先跑一轮只读规划：`codex exec -s read-only`。这一轮不使用你配置的写入沙箱。
4. 规划轮里的只读查看（`pwd`、`rg`、`sed -n` 这类）不会要求确认。规划给出可以直接朗读的结论时，这一轮就结束，并读出摘要。
   只有规划自己列出了写文件、删除、付款、部署、发送，或要在比 `read-only` 更宽的沙箱里再开一轮时，才停下来等确认。确认之后才另起执行进程。
5. 删除、付款、部署、发送，以及 `danger-full-access`，只能按确认键。说「确认」不算。其他动作可以说「确认」或按 `Ctrl+Shift+Y`。十秒内不确认就取消，过期的「确认」不会再开一轮。
6. 下一轮用保存的 thread id 走 `codex exec resume <id>`。只在这一轮成功结束（`turn.completed`）后才保存。不会使用 `resume --last`。
7. macOS `say` 合成摘要，`afplay` 播放。你再次开口或按下热键时，播放立刻停。

默认执行沙箱是 `read-only`。需要改仓库里的文件时，把配置改成 `workspace-write`。规划轮仍然是 `read-only`。`--yolo`、`--dangerously-bypass-approvals-and-sandbox` 一律拒绝。`--full-auto` 不是 Codex CLI 0.153.4 的参数，出现在参数里也会被拒绝。

```mermaid
flowchart LR
  Mic[无线领夹麦 USB 接收器] --> Audio[音频与 VAD]
  Key[按住说话 / 确认键] --> Audio
  Audio --> STT[本地 STT]
  STT --> Plan[codex exec 只读规划]
  Plan --> Need{要执行动作?}
  Need -->|否| TTS
  Need -->|是| Confirm[确认具体动作]
  Confirm --> Exec[另一个 codex exec 进程]
  Exec --> TTS[say 摘要]
  TTS --> Play[播放，可打断]
```

## 硬件

买一个带 USB 接收器的无线领夹麦，插上后 macOS 把它当成输入设备。播放用 Mac 自带扬声器，或有线耳机。价格是估计：

| 东西 | 说明 | 估价 |
| --- | --- | --- |
| 无线领夹麦 | USB 接收器，Mac 显示为输入设备。RØDE Wireless GO、大疆 Mic Mini，或国产 USB 款都可以。[京东搜索](https://search.jd.com/Search?keyword=%E6%97%A0%E7%BA%BF%E9%A2%86%E5%A4%B9%E9%BA%A6) | ¥150–1500 |
| 播放 | Mac 扬声器或已连接的耳机 | ¥0–300 |
| 可选会议麦 | 自带回声消除，外放时更不容易把自己的声音又录进去 | ¥300–800 |

麦走 USB，不要同时用蓝牙麦和蓝牙音箱，否则系统会掉到免提通话的音质。外放时，喇叭声可能被 VAD 当成你在插话。第一版用按住说话，或者戴耳机。

## 在 Mac 上安装

需要 Homebrew、Python 3.11 或更新版本，以及已经能在终端里运行的 Codex CLI。本方案按 `codex-cli 0.153.4`、路径 `/opt/homebrew/bin/codex` 来写。先确认：

```bash
/opt/homebrew/bin/codex --version
/opt/homebrew/bin/codex login status
```

`login status` 退出码为 0 才算登录过。没有的话执行 `codex login`。

```bash
brew install python@3.12
git clone https://github.com/AlexJCD2025/codex-voice-orb.git
cd codex-voice-orb
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[audio,stt,hotkey]"
mkdir -p ~/.codex-voice-orb
cp config.example.yaml ~/.codex-voice-orb/config.yaml
```

程序只自动读取 `~/.codex-voice-orb/config.yaml`。当前目录里的 `config.yaml` 不会生效，避免一份偶然放在仓库里的配置被当成信任配置。要指定别的文件，用 `orb --config /path/to/config.yaml`。

`codex.bin` 保持 `/opt/homebrew/bin/codex`。若 `which codex` 在别的位置，改成那个路径。`orb doctor` 会打印它实际解析到的绝对路径；文件名不是 `codex` 时只警告，不把任意可执行文件当成失败。

第一次转写会下载 faster-whisper 的 `small` 模型，能同时处理中文和英文。想更准，把 `stt.model` 改成 `medium` 或 `large-v3`。也可以改用 whisper.cpp：安装 `whisper-cli`，把 ggml 模型路径写到 `stt.model_path`，再把 `stt.engine` 设为 `whisper.cpp`。

### 系统权限

- 系统设置 → 隐私与安全性 → 麦克风：打开「终端」（或 iTerm，看你从哪个 App 启动）。
- 全局热键还要在同一页打开「输入监视」和「辅助功能」。终端开了 Secure Keyboard Entry 时，热键也会读不到。`orb doctor` 里的 hotkey 一项只是提醒：监听器能启动，不等于系统已经把按键交过来。要真的试一次按键，再跑 `orb doctor --hotkey`（它会等 5 秒）。
- 插上 USB 接收器。`orb doctor` 会打印输入设备名。若不用系统默认麦克风，把名字写进 `audio.input_device`。

## 配置

`config.example.yaml` 里每项都有注释。常用的只有这些：

- `audio.mode`：`ptt` 或 `vad`
- `audio.hotkey` / `audio.confirm_key`：按住说话，以及执行确认键。默认热键是 `ctrl+alt+space`
- `audio.max_utterance_s`：单段最长秒数，默认 30
- `codex.sandbox`：确认后的执行沙箱，`read-only` 或 `workspace-write`。规划轮固定是 `read-only`
- `codex.model`：传给 `codex exec -m`
- `codex.config_overrides`：只接受 `model`、`model_reasoning_effort`，以及 `sandbox_workspace_write.network_access=false`
- `codex.images` 或命令行 `--image`：传给 `--image`。执行轮如果续上了规划的 thread id，就不再重复传图片
- `codex.resume`：只能是 `id`。用上次成功保存的 thread id，不会调用 `resume --last`
- `tts.voice`：例如 `Ting-Ting`。不填就用系统默认声音

没有 `extra_args`。命令行是闭合的，不能从配置里再塞 `--add-dir`、`--sandbox` 或 `--config`。`permissions.allow_full_auto: true` 会让配置加载失败。`allow_danger_full_access` 默认是 `false`；只有显式打开，并且用户按了确认键，执行轮才能使用 `danger-full-access`。规划轮仍然是只读。

## 第一次真机测试

在插了麦、登录过 Codex 的 Mac 上按这个顺序做。前两步不需要热键。

1. 看环境缺什么：

   ```bash
   source .venv/bin/activate
   orb doctor
   ```

   麦克风、`codex` 路径、`codex login status`、STT 模型、`say` 都应该是 `pass`。STT 模型还没下载时是 `warn`，第一次转写会自己下。有 `fail` 就先按上一节处理，不要往下走。

2. 只测 Codex 和朗读，不测麦：

   ```bash
   orb once --text "用两句话说明当前目录是做什么的"
   ```

   终端会打出完整回复，扬声器或耳机会读一句摘要，退出码为 0。规划时 Codex 可以跑 `pwd`、`rg`、`sed -n` 这类只读命令，不该因此停下来等确认。如果它要求确认后才肯回答「这个目录是做什么的」，那是闸门把查看当成了执行。失败时先看 `codex login status`，以及 doctor 打印的 `codex.bin` 绝对路径。

3. 测多轮是不是同一条 Codex 会话：

   ```bash
   orb listen --stdin
   ```

   输入 `记住数字四十二`，回车，等它读完。再输入 `我刚才让你记的数字是多少`。第二轮的命令行里会带 `resume` 和上一轮的 thread id。空行退出。看会话：

   ```bash
   orb session show
   orb session clear
   ```

4. 测麦。按住 `Ctrl+Alt+Space`，说「这个目录里有哪些文件」，松开。听到「好。」之后等待，然后听到一句摘要。终端里同时有全文。

   ```bash
   orb listen
   ```

5. 测打断。等它开始朗读时，再按住热键说话。正在播的声音应马上停，新的一句进入下一轮。

6. 测高风险确认。说「把临时文件删掉」，或：

   ```bash
   orb once --text "把临时文件删掉"
   ```

   它会先做一轮只读规划，然后列出要执行的动作。删除只能按 `Ctrl+Shift+Y`，嘴上说「确认」不会启动执行进程。说「取消」，或干等十秒，执行进程不会出现。确认之后，执行进程用的是 `read-only` 或你配置的 `workspace-write`，不会变成 yolo。

7. 看延迟。每一轮追加一行 JSON，里面是各段毫秒数，没有原文：

   ```bash
   tail -f ~/.codex-voice-orb/metrics.jsonl
   ```

   字段包括 `stt_ms`、`permission_ms`、`codex_ms`、`tts_ms`、`playback_ms`、`total_ms`。

要改仓库里的文件时，把 `codex.sandbox` 改成 `workspace-write`，再从第 2 步做一次。规划轮仍是只读。删除、付款、部署、发消息要按确认键之后，才会有第二个进程。

## 命令

| 命令 | 作用 |
| --- | --- |
| `orb doctor` | 检查麦、Codex、登录、STT、TTS |
| `orb once --text "..."` | 跳过麦，直接问一轮。`--image` 可重复 |
| `orb once --text "..." --no-tts` | 只打印，不调用 `say` |
| `orb listen` | 热键或 VAD |
| `orb listen --stdin` | 用键盘打字，验证 resume |
| `orb session show` / `orb session clear` | 查看或忘掉 thread id |

配置文件可以用 `orb --config /path/to/config.yaml doctor` 指定。不带 `--config` 时不会读取 `./config.yaml`。

## 安全边界

闸门只管「要不要另起一个执行进程」。只读规划里的查看命令不是执行。

- 问「这个目录是做什么的」时，规划轮可以跑 `pwd`、`rg --files`、`sed -n`。这些 `command_execution` 事件出现在命令已经开始之后，而且只读沙箱本来就允许查看。它们不会弹出确认，回答会直接朗读。
- 确认出现在规划列出了具体的写、删、付款、部署、发送，或执行沙箱不是 `read-only`、因此第二轮可以改东西的时候。删除、付款、部署、发送仍然只能按确认键。
- 规划阶段如果已经跑了 `rm`、改了文件，或调用了会发消息的 MCP，日志同样晚于动作。这不能事后拦住。这种事件不会单独把回答藏起来，也不会自动再开一轮去重做。只有规划还要求执行时，确认文案里才会提到它们。
- 你自己配在 Codex 里的 MCP 仍可能在规划轮运行。0.153.4 没有一个可以单独关掉 MCP、又保留模型和登录的开关。规划提示会要求模型不要调用有副作用的工具，但这不是强制的。
- 关键词本身不会把沙箱切到 `workspace-write`。
- `--yolo` 和 `--dangerously-bypass-approvals-and-sandbox` 不会因为确认就放行。`allow_full_auto: true` 不能用来打开它们。

其它边界：

- 音频、whisper.cpp 的文本和 `say` 的 AIFF 用 `0600`，目录用 `0700`，用完就删。删不掉会打出警告。
- 指标日志只记耗时和字符数，不记原文。
- 发给 Codex 的是文字。语音不会被拼进 shell。
- 打断会先 SIGTERM 进程组，宽限之后 SIGKILL，并关掉管道，避免读 stdout 的线程一直挂着。已经执行完的命令不会自动撤销。
- 确认有期限和轮次。超时之后的「确认」不会新开一轮。含糊的「好」「嗯」「可以」不算确认。等待确认时麦克风是关上的，避免把提示音又听成同意。

## 开发

后端接口在 `orb/backends/base.py`。再接一个智能体时，实现 `plan()` 和 `execute()`，并在 `build_runtime()` 里换上。`execute()` 只能由编排器在确认之后调用。TTS 同样只要求 `synthesize(text, turn_id) -> 音频路径`。

单元测试用假的 Codex 进程，不连网，在 Ubuntu 上就能跑：

```bash
pip install -e ".[dev]"
python -m pytest -q
python -m orb doctor
```

GitHub Actions 在 `ubuntu-latest` 上跑 pytest，并确认 `doctor` 的 microphone、codex、stt、tts 四行检查都有结果。`macos-latest` 上再跑一遍 pytest，不要求麦克风。这台 Linux 机器上麦、`say` 和 Codex 不存在，`doctor` 会报告失败项，但命令本身要能跑完。

## 布局

```
orb/audio.py            采集、按住说话、RMS VAD
orb/stt.py              faster-whisper / whisper.cpp
orb/tts.py              say，可替换
orb/playback.py         播放与打断
orb/orchestrator.py     一轮的状态、确认、打断
orb/permissions.py      高风险粗筛和确认词
orb/session.py          thread id
orb/backends/codex_cli.py
orb/config.py  orb/cli.py  orb/metrics.py  orb/doctor.py
```
