# ATEQ 单仪器测试系统整改说明（离线验证版）

日期：2026-10-01。执行：OpenCode（opencode-go/deepseek-v4.1-flash，max）。
范围：F01..F11；隔离副本离线实现与测试。本文件说明已实现内容、接口变化、未确认协议与现场验收项。

## 1. 结论摘要

- 端口现状（用户 2026-10-01 现场确认）：仪器 COM8（9600/8E1/站号255，现场测试成功）；
  扫码枪 COM7（9600/8N1，现场测试成功）。活跃默认已由 COM1 更新为 COM8，扫码枪端口独立，
  `ATEQ_SERIAL_PORT` / `SCAN_SERIAL_PORT` 仍可分别覆盖。
- 离线回归：`python -m unittest discover -s tests -p "test_*.py"` 201 项全部通过（详见证据目录原始输出）。
- 本轮**不包含**任何真实串口/仪器、打印、现场 HTTP、数据库、开机启动操作；所有结论均为离线证据。
- 以下事项属于现场联机验收，本轮不能宣称通过：真实 RTU 响应、StepCode 结束语义、
  单位编码、状态位完整语义、参数(FC16)准备写入的实际含义、打印机出纸、扫码枪实际帧格式。

## 2. F01..F11 对照

| 编号 | 修复要点 | 主要实现 | 离线验证（测试） | 现场待验收 |
|---|---|---|---|---|
| F01 | 停止后不得再发启动/切程序；停止区分取消受理与复位确认 | `cycle_state.CycleManager.check_cancelled` 与周期标识；`program_selector.request_stop` 立即返回 `cancel_accepted/reset_pending/reset_confirmed`；命令锁 + 取消检查；`_perform_reset` 经单一优先级命令通道 | `test_stop_linearization.py`（6 项：锁被占用时 <1s 返回、取消后零启动、P2 前取消、复位确认、忙保持、命令时间戳） | 真实复位线圈 ACK 行为 |
| F02 | 程序2不得清空程序1；三种入口统一周期模型 | 单一 `CycleManager` + `CycleRecord`（p1/p2 阶段快照）；硬件监视只附加证据，只有新周期创建时才清空 | `test_program_cycle.py::test_two_programs_pass_and_p1_retained`、`test_cycle_state.py` | 真实硬件连续两次程序运行 |
| F03 | required_programs 冻结于配置；缺程序不得整体PASS | 冻结 `context.program1/program2` → `required_programs`；P1 非 PASS 跳过 P2 且记录 `skip_reason`；无 P2 证据不 PASS | `test_p2_missing_never_pass`、`test_p1_fail_skips_p2_and_never_prints` | 现场双程序配方确认 |
| F04 | 异常复位/65535 不得冒充完成；禁止伪造0值；测量只取同周期证据（见 6d 修订） | `_evaluate_phase`：必须 observed activity + 终止样本归属本阶段/本周期 + 程序号匹配 + 身份已验证 + status bit5 循环结束 + bit0 PASS + 无失败位/未知位；**压力只取同周期最后一次有效 StepCode 6，缺失 → `UNKNOWN(MISSING_PRESSURE_SAMPLE)`，绝不用 65535 回填；泄漏取同周期最后一次有效 StepCode 7，没有有效 7 时才回退同周期 65535，二者都缺失 → `UNKNOWN(MISSING_STEP7_LEAK)`**；stepcode 0 → ABORTED；缺失终止样本 → UNKNOWN；从未启动的 P2（硬件或链式超时）→ UNKNOWN 而非产品 FAIL | `test_abnormal_reset_is_aborted_not_pass`、`test_missing_sample_never_pass_and_no_fabricated_zero`、`test_terminal_missing_measurements_not_backfilled_from_running`、`test_same_number_two_phases_require_new_activity`、`test_p2_missing_never_pass`、`tests/test_protocol_amendment.py`（11 项） | 真实 StepCode/状态位组合、6/7 阶段时长与采样可达性 |
| F05 | written_program1 缓存不得当仪器事实 | 每次预备/启动均重新读回程序号；缓存仅作提示且不再短路写入；程序不符 → `PROGRAM_MISMATCH`/`PROGRAM_UNVERIFIED`，PASS 被阻止 | `test_unverified_program_blocks_pass`、`test_arm_next_context_idle_prepares_program`、`test_hardware_readiness_prepares_p1_after_p2_terminal` | 程序中读程序号可行性 |
| F06 | 扫码开关准入、幂等事件、原始码保留、有界队列、后台自主消费 | `serial_scanner` 事件 (session_id, seq, event_id)、有界队列+溢出可见且使绑定失效、保留完整原始码；后台回调统一处理（无浏览器也可）；`/api/scan_qualified` 幂等；开关仅由服务端决定；`scan_start` 才自动启动 | `test_scanner_events.py`（8 项）、`test_scan_admission_api.py`（10 项） | 扫码枪真实帧、条码规则（未配置） |
| F07 | 上下文冻结、准入互斥、忙态可见、重启标记 | `admission_lock` + 单活动周期；上下文入周期时冻结；`arm_next_context` 忙时只更新下一周期；快照走 `state_lock`；原子持久化（唯一 tmp+replace）；重启恢复标记 INTERRUPTED/UNKNOWN 且不自动重启/重打 | `test_cycle_state.py`（8 项）、`test_arm_next_context_defers_while_busy`、`test_startup_during_active_instrument_cycle_is_interrupted` | 现场断电/重启演练 |
| F08 | 状态服务不阻塞、缓存采样、单一串口所有者 | 移除 import 期杀进程/建库/启线程；显式 lifespan；`/api/sync_test_context` 改为同步线程池路由；`/api/data` 只读采样缓存；单仪器管理线程（采样+硬件检测+复位优先）；错误码 PORT_OPEN_FAILED/READ_TIMEOUT 等 | `test_import_safety.py`（2 项）、`test_scan_admission_api.py::test_slow_serial_does_not_delay_status_or_stop`、`test_api_contracts.py::test_data_serves_cache_and_reports_no_sample` | 真实串口打开/超时表现 |
| F09 | 参数站号、整体互斥、失败不冒成功 | `/api/params` 首帧使用 `STATION_ID` 且**选择帧地址保持原实现 0x3004**（不猜测其他寄存器）；整段三帧在 `modbus transaction` + IDLE 预留 + 优先级总线门内；失败返回 `success:false` 且不返回 3/2/5 默认值；成功缓存标记 `cached/stale/program`；FC16 异常帧不作为成功 | `test_review_fixes_api.py::test_selector_frame_uses_original_address_0x3004`、`test_api_contracts.py::test_params_*`、`test_modbus_utils.py::test_exception_frame_is_not_truthy_success` | 参数准备写入的真实语义与允许时机 |
| F10 | 周期唯一记录、原子日流水、先存后打、补打复用、打印锁/超时 | `cycle_id` 唯一索引 + 幂等保存；`BEGIN IMMEDIATE` 同事务分配日流水；**先写入最终结果/原因/时间再保存，先安装 record_id/serial 再打印**；保存失败/发号失败/幂等重复绝不打印；`/api/print_label` 必须携带已保存 PASS `record_id` 并复用序列号与原始模板/供应商；**记录级 claim 覆盖“保存→提交”，停止在共享打印锁内、文件/进程提交前再次检查取消谓词**；打印锁覆盖数据文件+进程，有限超时 | `test_review_fixes.py::R1*`、`R12*`、`test_database.py`（6 项）、`test_print_flow.py`（11 项） | 打印机出纸与 BarTender 行为 |
| F11 | 服务端产品配置、快照显示、单位保留、无伪动作 | 新增 `product_config.py`（原子写+revision+旧 program→program1 迁移，不猜 program2）；P1/P2 结果取自后台冻结快照；单位按编码保存展示；`switchChamber` 标记“未实现”；旧 `/api/start` 返回明确兼容错误，不再 Popen 抢串口；UI 保持四列布局 | `test_product_config.py`（7 项）、`test_js_behavior.py`（4 项，含 Node 行为校验）、`test_parsing.py`（8 项） | 多客户端现场使用、switchChamber 硬件 |

## 3. 新增/变更接口

新增：
- `GET/PUT /api/product_config`：服务端产品配置；PUT 带 `revision`，冲突返回 `REVISION_CONFLICT`；
  响应含 `switch_chamber_implemented:false`。
- `/api/params` 失败响应新增 `error_code/message/cached`，成功新增 `cached/stale/sampled_at`；不再伪造默认值。
- `/api/test_status` 新增：`cycle_id, busy, trigger, required_programs, terminal_reason,
  admission_violation, actual_program1/2, pressure*/leak*_unit, phase1/2_status, skip_reason2,
  sample{...}, reset{...}, prepare{...}, supervisor{...}, record_id, print_state`。
- `/api/stop_test` 新增：`cancel_accepted, reset_confirmed, reset_pending, reset_state, reset_error, cycle_id`。
- `/api/scanner/status` 新增：`session_id, event_id, pending_count, dropped_count, overflowed,
  processed_seq, last_result, binding, binding_invalid, queue_max`。
- `/api/data` 改为缓存读取，新增 `sampled_at, stale, error_code`。
- `/api/records` 创建支持可选 `cycle_id`；记录新增列 `cycle_id, program*_actual, *_result,
  *_unit, terminal_reason, started_at, finished_at, print_state, print_message,
  print_attempted_at, label_template, supplier_code`。

保留兼容：`/api/products` 形状不变（服务端配置优先，回退数据库型号）；原有记录/查询/导出路由
形状不变；`save_test_record(...)` 仍返回整数 record id；`/api/scan_qualified` 保留且兼容无 event_id 的
手动录入（标记 `device_verified:false`）。

## 4. 数据模型与迁移

- `test_records` 仅新增可空列（见上），不删表不删行；`cycle_id` 建唯一索引（旧行 NULL 不受影响）。
- 日流水在 `BEGIN IMMEDIATE` 事务内 `MAX(daily_serial)+1` 后随记录同事务插入。
- `product_models.json` 只读迁移：`program → program1`，`program2=0`（不猜测），写入 `products.json`。
- 旧的 `ateq_modbus.py` / `ateq_response_parser.py` / 根脚本未修改。

## 5. 锁顺序与停止线性化（实现契约）

- 偏序：`admission_lock → 总线优先级门(RLock) → modbus_lock(RLock) → write_commit_lock`；
  `state_lock` 为叶锁，只复制快照、不做 IO；`write_commit_lock` 只在“写请求字节”的极短临界区内持有，
  响应读取/网络读在其外。`request_stop` 先置取消事件，再以 `commit_barrier()` 取得同一把写提交锁，
  从而与每个状态改变帧的提交形成唯一线性化点：STOP 先得锁 ⇒ 后续帧拒绝写出；帧先提交 ⇒ 记为在途，
  ON 已提交时 OFF 清理照发。
- **所有总线操作（读取/写程序/启动/复位/参数事务）统一经过一个优先级门**：复位为优先等待者，普通读/命令在
  有优先等待时先让路，避免复位被连续读饿死；取消检查在**获得总线所有权之后、任何传输写/启动之前**执行。
- `request_stop()`：在同一状态锁内原子地置周期取消事件 + `reset=pending`，因此停止返回后不存在“未加禁入就放行新周期”的窗口；
  复位经优先级通道执行；带限时总线等待。
- `_perform_reset`：复位脉冲后**必须再读到空闲 StepCode(0/65535) 才置 confirmed**；仅 ACK 或仍处于活动态 → failed，
  并且 **pending/failed 状态都会阻止新周期、外部预留与程序准备**，直到一次成功的空闲证明（可重试，不永久卡死）。
- 工作线程每个命令/读之前重新检查取消与周期身份；旧周期线程无法修改新周期状态。已被写入/启动的帧属于在途命令
  （含 start 脉冲的 OFF 清理），不做“未发送”的虚假声明；停止之后不再发出新的写程序/启动帧。

## 6. 未确认协议与现场验收清单

| # | 假设 | 现场验证动作 |
|---|---|---|
| P-1 | 实时块 FC03 0x0030 13 寄存器、程序/压力/泄漏/单位字节序 | 与手册/仪器显示逐项比对 |
| P-2 | 状态位：bit0 PASS、bit5 循环结束、0x001E 失败掩码、bit15 键盘锁，未知位阻止 PASS | 用已知合格/不合格件抓取状态字 |
| P-3 | 仓库内三种状态位解释不一致（webui/ateq_modbus/F5 手册），本轮采用保守掩码 | 记录真实位含义后再固化 |
| P-4 | StepCode 65535=唯一终止/空闲标记；**65525 不识别**；0=复位（非结果/非空闲证据）；4..7 活动，其中 6=压力源、7=泄漏优先源、65535=无 7 时的泄漏回退源 | 记录一次完整件的 StepCode 序列（重点 6→7→65535 与 0 复位） |
| P-5 | 活动测试期间程序号可读回 | 在 4/5/6 阶段读程序号 |
| P-6 | FC16 参数准备/读取协议：选择帧地址沿用原实现 0x3004、首帧站号 STATION_ID | 逐个程序读取参数并与面板核对 |
| P-7 | 单位码表完整 | 与仪器单位显示比对 |
| P-8 | 参数准备写入在空闲时才安全 | 现场确认后再启用写准备 |
| P-9 | 扫码枪帧格式/分隔符 | 实枪扫码验证 |
| P-10 | BarTender 退出码 0 仅代表命令提交 | 人工确认出纸 |
| P-11 | 程序中写程序被仪器拒绝 | 现场复核 |
| P-12 | 无 PLC，无法物理互锁；无有效扫码的硬件启动记为 ADMISSION_VIOLATION，不打印不合格品 | 现场流程确认 |

## 6b. 终审修复轮 R1..R12（同一会话）

| # | 修复 | 关键测试 |
|---|---|---|
| R1 | 终结顺序：先写 overall_result/terminal_reason/finished_at 再调用 recorder；先安装 record_id/serial/created 再调用 printer；exactly-once 守卫（含并发 abort/completion）；recorder/printer 异常被捕获并写入 record_error/print_state；`created` 决定是否打印，重复完成不重发号不重打 | `test_review_fixes.py::R1*`（含真实 SQLite + 生产默认保存器 + 注入 BarTender runner 集成） |
| R2 | 所有总线操作过单一优先级调度门；取消检查位于“获得总线后、传输写/启动前”；P2 写后再次检查；等待总线期间停止不再发新帧；复位优先级不被排队读饿死 | `R2*`、`RealBusBoundaryTests`（真实 DeviceAdapter + 注入 transport：预写读阻塞、等锁、P2 写中取消、无 stop 后新启动帧） |
| R3 | 每阶段终止样本必须 `actual_program == 该阶段期望程序` 且身份已验证；运行中程序变化永久失效该阶段身份；终止样本缺失测量不得用运行旧值回填；P1=P2 时 P2 必须重新观察活动；P2 期间 P1 快照不可变 | `R3*`（含 same-number、mid-phase mutation、terminal mismatch） |
| R4 | 首次活动样本若未先观察到确认空闲 → 周期标记 `INSTRUMENT_STATE_UNVERIFIED`，不 PASS/不链式/不打印；程序准备在“空闲证明+写+回读”全程持有 IDLE 预留；程序写入拒绝所有活动 StepCode 4..100 | `R4*` |
| R5 | 硬件 P2 保持 CHAINING 直到真实活动；链超时/阶段超时/静默 → UNKNOWN/ERROR（非产品 FAIL）；supervisor 的终止走异步一次性线程，不阻塞采样 | `R5*`、`test_hardware_phase_watchdog_times_out_unknown` |
| R6 | reset failed 同样阻止准入/预留；ACK 后需空闲 StepCode 才 confirmed；停止在返回前原子安装 reset pending；带重试恢复，不永久卡死 | `R6*`、`tests.test_cycle_state` |
| R7 | 硬件周期使用**已消费绑定**的完整 code/source/device_verified（scan 可选时也用于标识工件）；消费与准入单次；忙碌扫码按 BUSY 消费，不带入下一件 | `R7*`、`tests.test_scanner_events` |
| R8 | 软件启动程序/模板/供应商一律来自服务端产品库，未知/删除/损坏 → 明确拒绝；arm_next_context 显式整体替换下一周期上下文并失效就绪；PUT 严格校验（含 program2 越界、重复型号），迁移对显式非法 P2 跳过并告警而非静默清零 | `R8*`、`tests.test_product_config` |
| R9 | 参数选择帧恢复原地址 0x3004（仅站号改为 STATION_ID、CRC 保持）；crafted 帧断言精确地址 | `test_selector_frame_uses_original_address_0x3004` |
| R10 | 单位码按完整 32 位高低字解码（高位不再与低位重叠）；生产适配器返回类型化错误；采样缓存当前失败时 `/api/data` success:false，last-good 仅以 stale 标注保留 | `R10*`、`tests.test_parsing` |
| R11 | lifespan 数据库初始化失败 → 置 STARTUP_ERROR 且不启动任何硬件/控制；持久化失败可见并阻止准入；中断恢复记录保存失败保留 `.interrupted.json` 证据；`ATEQ_DISABLE_HARDWARE=1` 下导入不再创建任何日志文件 | `R11*`、`tests.test_import_safety` |
| R12 | 自动打印携带取消谓词，在共享打印锁内、文件/进程提交前复检；记录级 claim 覆盖“保存→提交”，手动补打在自动任务在途时返回 BUSY；已提交才如实报告 submitted（不代表出纸） | `R12*` |

## 6c. 第二轮修复 B1..B4 与 COM8（同一会话）

| # | 修复 | 关键测试/复现 |
|---|---|---|
| B1 | 取消与发送提交的原子线性化（Codex 定向复审后补强）：新增 `modbus_utils._write_commit_lock` + 线程本地 `send_guard(predicate)` + `commit_barrier()`；取消谓词在**传输写提交临界区内、实际写之前**判定，响应读取在锁外；`request_stop` 在返回前调用 `commit_barrier()`，因此 STOP 返回后不可能再有新的 FC06/ON 提交；若帧已提交则视为在途，ON 已提交时 OFF 作为强制清理（不受取消谓词约束）；FC06 提交后不再回读/不再启动；旧脚本包装函数即使丢弃 kwargs 也因线程本地 guard 仍生效；**TCP 路径取消结果字典在响应校验前直接透传（`send_raw` 仍返回 None，不创建 socket）；取消谓词抛异常时 fail-closed（拒绝发送，绝不视为放行）** | `tests/test_third_round.py`（评审同款 send 屏障：FC06/ON stop 先胜、已提交逆序、ON→OFF 清理、guard 单测、TCP 取消无 socket、谓词异常 fail-closed）、Codex 屏障脚本 `codex_b1_send_commit_race.py`（exit 0，`fc06_frames: []`）、`codex_tcp_cancel_repro.py`（exit 0，返回 CANCELLED）、`tests/test_second_round.py::B1*`、`verification/round2_scripts/b1_stop_fc06_commit.py` |
| B2 | 总线门票仲裁器：等待者在有序队列登记（priority 在前、同类 FIFO），仅在队首时物理获取 `mutex`；等待期间若有 reset 到达则让位重排；超时/异常撤票不锁死总线 | `B2*`（normal 先到、reset 后到仍 reset 先获授权；8 并发普通请求不饿死 reset；超时后可继续使用）、`b2_reset_priority_arbitration.py` |
| B3 | 可选扫码无当前绑定即空码：硬件周期 QR 仅取“本次消费的绑定”，不再回退旧 pending；开始/完成/停止清 pending QR，一次绑定最多消费一次 | `B3*`（stale pending 不复用、新绑定生效、完成/停止清码、显式 software QR 保留）、`b3_optional_scan_binding.py` |
| B4 | 跨重启持久的记录级打印准入：production saver 在 INSERT 时即写 `auto_pending`（PASS），手动补打只能从终态原子领取 `manual_pending`；自动/手动互斥由 SQLite 原子 UPDATE 判定；启动恢复把 pending 置 `unknown` 且不自动重试；失败/超时/禁用/纸张未证等状态持久保存 | `B4*`（保存插入→返回窗口并发 manual=busy 且 runner 恰 1 次；自动处理中 busy；自动结束后允许一次显式补打；重启不重试）、`b4_persistent_print_admission.py` |
| COM8 | 仪器默认端口 COM1→COM8（9600/8E1/255），`ATEQ_SERIAL_PORT` 仍可覆盖；扫码枪保持 COM7/9600/8N1 且独立可覆盖；仅活跃默认与文档更新，旧脚本内 COM1/COM4 标注为不由 WebUI 调用 | `Com8DefaultTests`（常量、独立 env 覆盖、fake serial 断言构造参数 port=COM8，无真实端口访问） |

## 6d. 现场协议修订 A1（2026-10-26 再次确认测量来源）

本节取代此前对终止码与测量来源的全部解释（含 65525 及“压力取 7、泄漏取 6”的旧实现）。

实现规则：

- **只承认 StepCode 65535 为终止/空闲标记**；65525 不识别。StepCode 0 仅按异常复位处理，
  绝不作为测试结果。复位确认路径接受 0/65535，但仅 65535 能置位硬件空闲同步。
- **65535 单帧永远不是结果**：必须同一周期观察到活动 StepCode，且终止样本具备当前阶段的
  有效程序号与状态字（bit5 循环结束 + bit0 PASS + 无失败/未知位）。
- **压力来源**：同一周期最后一次有效 StepCode 6 采样；后续无效采样不覆盖已有有效值。
  不使用 65535、StepCode 7、其他运行阶段或上一周期旧值回填；缺失时为
  `UNKNOWN(MISSING_PRESSURE_SAMPLE)`。
- **泄漏来源**：同一周期最后一次有效 StepCode 7 采样；没有有效 StepCode 7 泄漏时，才使用
  同周期 65535 终止帧的泄漏值及单位。两者都缺失时为 `UNKNOWN(MISSING_STEP7_LEAK)`。
- **Modbus 异常中文化**：异常帧仍是类型化失败，绝不当成功。
- **前端监控同步**：压力实时采集 4..6，进入 7 后保持最后的 6 阶段压力；泄漏实时采集 7/65535；
  曲线在 6→7 时继续记录，结果判定始终以服务端同周期证据为准。

实现切片：`cycle_state.PhaseSnapshot` 使用
`pressure_step6/pressure_step6_unit/leak_step7/leak_step7_unit`；
`program_selector._cycle_evidence_fields(step, sample)` 按观测到的 StepCode 记录最后有效证据，
软件等待路径与硬件 supervisor 路径共用；`_evaluate_phase` 统一解析证据并处理泄漏的 65535 回退；
`webui_server` 同步实时面板、曲线采集条件和缺失泄漏原因显示。

协议测试（`tests/test_protocol_amendment.py`，11 项）覆盖：最后有效 6 压力与 7 泄漏、
65535 不覆盖已有 7 泄漏、缺 6 压力不回退、缺 7 泄漏使用 65535 及其单位、两类泄漏证据都缺失、
P1/P2 证据隔离、硬件周期取值、65535 终止语义、复位同步和 Modbus 异常中文化。

未验证的现场字段限制（不可离线确认，现场需复核）：

- 真实仪器 StepCode 6/7 的持续时间与采样可达性：错过 6 压力时为 UNKNOWN；错过 7 泄漏时
  只能使用同周期 65535 泄漏，二者都缺失时为 UNKNOWN。
- 65535 终止采样中泄漏字段的物理含义与稳定性（仅作为无 StepCode 7 时的回退来源）。
- 真实异常帧码值与标准映射是否一致（现场抓取异常帧核对中文消息）。
- StepCode 0 在复位路径中的精确语义（0/65535 均可作复位确认证明；硬件空闲同步仅 65535 置位，见 6e）。
- 真实 COM8/COM7、打印出纸、参数写入语义仍按第 6 节 P-1..P-12 现场验收。

## 6e. 复位确认与硬件同步分离 + 陈旧同步清除（最小安全修正，2026-10-01）

历史问题一：`TestExecutor._perform_reset` 在复位后读到 StepCode 0 或 65535 时都会置
`_hardware_synced=True`，导致 0 号“异常复位”确认即可放行外部硬件周期（准入不带
`INSTRUMENT_STATE_UNVERIFIED`），而现场协议只承认 65535 为空闲同步证据。

问题二（陈旧状态漏洞）：上一版修正把置位收紧为 `if step == STEP_COMPLETE: self._hardware_synced = True`，
但 0 号复位确认时**不清除**既有的 `_hardware_synced`。若 executor 此前已同步（例如 supervisor
观察到过 65535），随后一次 0 号“异常复位”确认后 `_hardware_synced` 仍为 True，外部硬件周期
仍可无 `INSTRUMENT_STATE_UNVERIFIED` 放行——同步证据已经陈旧，与 6d/6e 的
“仅 65535 是空闲同步证据”相矛盾。

修正（仅 `program_selector._perform_reset` 的 RESET_CONFIRMED 分支，其余逻辑不动）：

- `step in (0, 65535)` 仍置 `RESET_CONFIRMED`（停止线性化/复位确认语义保持）；
- 置位改为显式赋值 `self._hardware_synced = (step == STEP_COMPLETE)`：
  `step == 65535` 置 True，`step == 0` **显式清空陈旧同步**置 False；
- 0 号确认后若 supervisor 尚未观察到 65535，外部硬件周期继续按
  `INSTRUMENT_STATE_UNVERIFIED` 处理；supervisor 观察到 65535 时既有的
  `_handle_hardware_sample` 路径重新置位同步。

回归测试（`tests/test_protocol_amendment.py::test_reset_confirmed_at_step0_does_not_sync_until_65535`，
在原有断言前补充陈旧态前置条件）：同一 executor 上**先人为置 `executor._hardware_synced = True`**
（模拟此前已观察过 65535）→ 以 0 复位 → 断言 `RESET_CONFIRMED` 且 `_hardware_synced is False`
（陈旧同步被清除），随后硬件活动帧的准入被标记 `INSTRUMENT_STATE_UNVERIFIED`；再观察 65535 帧 →
`_hardware_synced is True`，随后的硬件活动帧准入无违规（`admission_violation is None`）。

失败优先证据：临时还原旧行为（仍为 `if step == STEP_COMPLETE: self._hardware_synced = True`，
0 号确认不清陈旧态）运行扩展后的测试 → `AssertionError: True is not false :
reset confirmed at step 0 must clear the stale hardware sync`（FAILED）；恢复显式赋值 → OK。

验证命令与结果（离线；仅 fake transport / 临时数据库 / 假打印机，未触碰现场）：

1. `python -m unittest tests.test_protocol_amendment tests.test_modbus_utils tests.test_program_cycle tests.test_cycle_state`
   → `Ran 46 tests ... OK`
2. `python -m unittest tests.test_stop_linearization tests.test_review_fixes` → `Ran 39 tests ... OK`
3. `python -m unittest discover -s tests -p "test_*.py"` → `Ran 201 tests ... OK`
4. `python -m py_compile program_selector.py modbus_utils.py webui_server.py cycle_state.py tests/test_protocol_amendment.py tests/test_modbus_utils.py`
   → 退出码 0（PY_COMPILE_OK）
5. `python tests\extract_js.py` → `wrote ...\embedded_ui.js (140803 chars)`；
   `node --check ..\embedded_ui.js` → NODE_CHECK_OK
6. `git status --short`：仅既有 8 个已修改跟踪文件与未跟踪新增目录/文件（本切片只动了
   `program_selector.py`、`tests/test_protocol_amendment.py`、本文件）；无现场/服务/部署改动。

## 7. 明确未实现/未启用

- `switchChamber` 无执行器：仅保存配置，界面标注未实现，不执行任何腔体动作。
- 条码规则未配置：仅保留完整字符串，不做型号匹配判定，记录中标注来源与 `device_verified`。
- 无物理互锁声明。
- 生产发布、现场数据库/打印/串口读写不在本轮范围。
