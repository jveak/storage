# Dual-Tier SSD KVCache 实验方案

## 1. 实验目标

本实验用于凸显 dual-tier SSD 在 LLM KVCache offload 场景下的产品优势。

dual-tier SSD 在同一块 SSD 内部将 NAND 颗粒动态划分为 SLC 区和 TLC 区，并支持 SLC/TLC 之间的数据在 SSD 内部搬运。实验利用 MLPerf KVCache benchmark 的路径分层能力，将：

- `--storage-cache-dir` 映射为 SLC 区
- `--cache-dir` 映射为 TLC 区

通过对比纯 SLC、纯 TLC、SLC/TLC 混合三种配置，从用户响应速度和容量效率两个角度评估产品价值。

核心目标：

- 证明 dual-tier SSD 对 interactive/responsive 用户可以提供接近 SLC 的低延迟。
- 证明 dual-tier SSD 对 batch/cold 数据可以利用 TLC 提供更高容量。
- 证明 dual-tier SSD 在混合业务下比纯 TLC 有更好的用户体验，比纯 SLC 有更好的容量弹性。
- 给出最大可支撑用户数、QoS 达标率、尾延迟和容量利用之间的对比。

## 2. 实验假设

KVCache workload 中不同 JOB 类型对存储的诉求不同：

| JOB 类型 | 用户特征 | 存储诉求 | 推荐落点 |
|---|---|---|---|
| interactive | 在线交互，强用户感知 | 极低尾延迟 | SLC |
| responsive | 一般在线请求 | 较低尾延迟 | SLC |
| batch | 后台批处理，弱用户感知 | 大容量和吞吐 | TLC |

因此，dual-tier SSD 的优势不应只用平均吞吐衡量，而应重点看：

- interactive / responsive 的 P95、P99、P99.9 延迟
- QoS 达标率
- 同等 QoS 约束下的最大用户数
- batch throughput
- SLC/TLC 容量占用分布

## 3. 实验配置

### 3.1 对照组

| 组别 | SSD 模式 | 容量配置 | MLPerf 路径映射 | 预期特点 |
|---|---:|---:|---|---|
| A | Pure SLC | 100G | SLC 作为全部 offload 路径 | 延迟最低，但容量小 |
| B | Pure TLC | 300G | TLC 作为全部 offload 路径 | 容量最大，但尾延迟较差 |
| C | Dual-tier SLC/TLC | 50G / 150G | SLC 承接 `--storage-cache-dir`，TLC 承接 `--cache-dir` | 兼顾响应速度和容量 |

### 3.2 路径约定

假设 SSD 模式切换后，系统提供如下挂载点：

```bash
/mnt/slc_kvcache
/mnt/tlc_kvcache
```

对应关系：

```bash
--storage-cache-dir /mnt/slc_kvcache
--cache-dir /mnt/tlc_kvcache
```

Pure SLC 模式：

- `--storage-cache-dir` 指向 SLC 路径
- `--cache-dir` 也指向 SLC 路径
- 或者只配置一个 SLC offload 路径，但需要保证所有 JOB 都落到 SLC

Pure TLC 模式：

- 不启用 `--storage-cache-dir`
- 所有 JOB 通过 `--cache-dir` 落到 TLC

Dual-tier 模式：

- `interactive` / `responsive` JOB 通过 `--storage-cache-dir` 落到 SLC
- `batch` JOB 通过 `--cache-dir` 落到 TLC

## 4. Workload 设计

实验建议使用三类 workload，分别突出低延迟、混合业务和容量压力。

### 4.1 交互优先 workload

目的：突出在线用户响应速度。

建议 JOB 分布：

| JOB 类型 | 占比 |
|---|---:|
| interactive | 40% |
| responsive | 30% |
| batch | 30% |

观察指标：

- interactive P95 / P99 / P99.9 latency
- responsive P95 / P99 latency
- QoS compliance rate
- requests/sec

预期结果：

- Pure SLC：尾延迟最好，但容量压力较早出现。
- Pure TLC：容量足够，但 interactive / responsive 延迟明显变差。
- Dual-tier：interactive / responsive 延迟接近 Pure SLC，同时保留 TLC 容量空间。

### 4.2 混合生产 workload

目的：模拟真实线上服务和后台任务共存。

建议 JOB 分布：

| JOB 类型 | 占比 |
|---|---:|
| interactive | 15% |
| responsive | 35% |
| batch | 50% |

观察指标：

- 不同 JOB 类型的 P95 / P99 延迟
- 整体 requests/sec
- batch throughput
- QoS compliance rate
- `storage_cache_entries`
- `disk_entries`
- `offloads_storage_cache`
- `offloads_disk`

预期结果：

- Pure TLC：整体容量表现较好，但在线 JOB 的 QoS 更容易失效。
- Pure SLC：在线 JOB 表现最好，但 batch 数据会快速消耗 SLC 容量。
- Dual-tier：在线 JOB 使用 SLC，batch 使用 TLC，整体用户体验和容量利用更均衡。

### 4.3 SLC 压力 workload

目的：验证 SLC 容量受限时 dual-tier 的弹性。

实验方式：

- 使用 Dual-tier 50G / 150G 配置。
- 逐步增加并发用户数或上下文长度。
- 让 SLC 区接近 50G 压力。
- batch/cold 数据持续进入 TLC。

观察指标：

- interactive P99 是否稳定
- SLC 使用量是否接近上限
- TLC 是否承接 batch/cold 数据
- QoS 达标率下降拐点
- 最大可支撑用户数

预期结果：

- Pure SLC 在 100G 容量压力下更容易出现空间不足或高频 eviction。
- Pure TLC 有容量但尾延迟较差。
- Dual-tier 利用 TLC 吸收容量压力，使 SLC 聚焦服务高优先级热数据。

## 5. 推荐实验流程

### 5.1 准备阶段

每轮实验前执行：

1. 切换 SSD 模式。
2. 清空测试路径。
3. 确认路径挂载和容量。
4. 记录 SSD 模式、SLC/TLC 容量、固件版本和测试时间。
5. 固定 benchmark 参数，包括模型、随机种子、duration、用户数扫描范围。

建议固定基础参数：

```bash
--model llama3.1-8b
--duration 300
--gpu-mem-gb 0
--cpu-mem-gb 0
--generation-mode none
--performance-profile latency
--seed 42
```

`--gpu-mem-gb 0` 和 `--cpu-mem-gb 0` 的目的是让 KVCache offload 压力直接落到 SSD，减少 GPU/CPU tier 对结果的干扰。

### 5.2 用户数扫描

对每种 SSD 配置运行相同用户数扫描：

```text
num-users = 10, 20, 40, 80, 120, 160
```

每个点建议运行 3 次，取中位数。

停止条件可定义为：

- interactive P99 > 200 ms
- 或 QoS compliance < 95%
- 或请求失败率明显上升

最大可支撑用户数定义为：

> 在满足 QoS 约束下，系统可稳定完成请求的最大 `num-users`。

### 5.3 容量压力扫描

保持用户数固定，逐步增加上下文长度或请求总量：

```text
context pressure = low, medium, high, saturation
```

目标是观察：

- Pure SLC 何时受容量限制。
- Pure TLC 何时出现尾延迟恶化。
- Dual-tier 是否能把在线 JOB 保持在 SLC，同时将 batch/cold 数据吸收到 TLC。

## 6. 命令模板

### 6.1 Pure TLC 300G

```bash
python3 -m kv_cache.cli \
  --model llama3.1-8b \
  --num-users <N> \
  --duration 300 \
  --gpu-mem-gb 0 \
  --cpu-mem-gb 0 \
  --cache-dir /mnt/tlc_kvcache \
  --storage-capacity-gb 300 \
  --generation-mode none \
  --performance-profile latency \
  --seed 42 \
  --output results_pure_tlc_users_<N>.json
```

### 6.2 Pure SLC 100G

```bash
python3 -m kv_cache.cli \
  --model llama3.1-8b \
  --num-users <N> \
  --duration 300 \
  --gpu-mem-gb 0 \
  --cpu-mem-gb 0 \
  --storage-cache-dir /mnt/slc_kvcache \
  --cache-dir /mnt/slc_kvcache \
  --storage-capacity-gb 100 \
  --generation-mode none \
  --performance-profile latency \
  --seed 42 \
  --output results_pure_slc_users_<N>.json
```

### 6.3 Dual-tier SLC/TLC 50G/150G

```bash
python3 -m kv_cache.cli \
  --model llama3.1-8b \
  --num-users <N> \
  --duration 300 \
  --gpu-mem-gb 0 \
  --cpu-mem-gb 0 \
  --storage-cache-dir /mnt/slc_kvcache \
  --cache-dir /mnt/tlc_kvcache \
  --storage-capacity-gb 150 \
  --generation-mode none \
  --performance-profile latency \
  --seed 42 \
  --output results_dual_tier_users_<N>.json
```

说明：

- `--storage-capacity-gb` 当前用于文件后端容量约束。Dual-tier 场景下建议同时记录真实 SLC/TLC 可用容量。
- 如果 benchmark 需要分别约束 SLC/TLC 容量，后续可扩展单独的 `--storage-cache-capacity-gb` 参数。

## 7. 关键指标

### 7.1 用户体验指标

| 指标 | 含义 | 价值 |
|---|---|---|
| interactive P95 / P99 / P99.9 | 在线交互尾延迟 | 直接反映用户体感 |
| responsive P95 / P99 | 普通在线请求尾延迟 | 反映服务稳定性 |
| QoS compliance rate | 满足 SLA 的请求比例 | 适合做产品卖点 |
| max supported users | QoS 达标下最大用户数 | 反映可承载能力 |

### 7.2 存储分层指标

| 指标 | 含义 |
|---|---|
| `storage_cache_entries` | SLC 层中的 KVCache entry 数 |
| `disk_entries` | TLC 层中的 KVCache entry 数 |
| `offloads_storage_cache` | offload 到 SLC 的次数 |
| `offloads_disk` | offload 到 TLC 的次数 |
| `storage_cache_path` | SLC 路径 |
| `disk_path` | TLC 路径 |
| storage read/write P95 | 存储读写尾延迟 |
| tier storage bandwidth | 存储层读写带宽 |

### 7.3 容量效率指标

| 指标 | 含义 |
|---|---|
| total write GB | 总写入量 |
| total read GB | 总读取量 |
| SLC occupancy | SLC 使用量 |
| TLC occupancy | TLC 使用量 |
| QoS per GB | 单位容量下的 QoS 能力 |

## 8. 结果汇总模板

### 8.1 用户数扫描结果

| 配置 | 容量 | 用户数 | Interactive P99 | Responsive P99 | Batch Throughput | QoS 达标率 | 结论 |
|---|---:|---:|---:|---:|---:|---:|---|
| Pure SLC | 100G |  |  |  |  |  |  |
| Pure TLC | 300G |  |  |  |  |  |  |
| Dual-tier | 50G/150G |  |  |  |  |  |  |

### 8.2 最大可支撑用户数

| 配置 | 最大用户数 | 限制因素 | 产品解释 |
|---|---:|---|---|
| Pure SLC |  | 容量 | 快但容量小 |
| Pure TLC |  | 延迟 | 容量大但用户体验差 |
| Dual-tier |  | SLC 热数据空间或 TLC 吞吐 | 响应和容量平衡 |

### 8.3 分层命中和 offload 分布

| 配置 | SLC entries | TLC entries | Offload to SLC | Offload to TLC | 说明 |
|---|---:|---:|---:|---:|---|
| Pure SLC |  |  |  |  |  |
| Pure TLC |  |  |  |  |  |
| Dual-tier |  |  |  |  |  |

## 9. 推荐图表

建议最终报告输出以下图表：

1. `num-users` vs interactive P99 latency
2. `num-users` vs responsive P99 latency
3. `num-users` vs QoS compliance rate
4. `num-users` vs requests/sec
5. SLC/TLC entry distribution
6. SLC/TLC capacity usage over time
7. batch throughput under each SSD mode

其中最能凸显产品优势的图通常是：

- interactive P99 latency 对比图
- QoS 达标最大用户数对比图
- SLC/TLC 容量分布图

## 10. 预期结论

Pure SLC：

- 优点：低延迟表现最好。
- 缺点：容量只有 100G，在长上下文或高并发下容易被容量限制。

Pure TLC：

- 优点：容量最大，batch 数据承载能力强。
- 缺点：interactive / responsive 尾延迟较差，QoS 更容易失效。

Dual-tier SSD：

- interactive / responsive JOB 落到 SLC，获得接近 Pure SLC 的响应速度。
- batch JOB 落到 TLC，获得接近 Pure TLC 的容量能力。
- 在混合 workload 下，能同时维持用户体验和容量效率。

最终产品卖点：

> Dual-tier SSD 在混合推理 workload 下，把低延迟资源优先给在线用户，把大容量资源留给批处理和冷数据，因此比纯 TLC 更快，比纯 SLC 更能支撑容量和并发。

## 11. 注意事项

- 每轮实验前需要确认 SSD 模式切换成功，并记录实际可用容量。
- 每个实验点至少运行 3 次，取中位数，避免单次抖动影响结论。
- 如果使用 `/tmp` 或普通文件系统模拟路径，延迟结果不能代表真实 SSD。
- 如果 SSD 内部 SLC/TLC 动态调整策略不可观测，需要额外采集设备侧 telemetry，以解释性能拐点。
- benchmark 当前可以通过路径区分 SLC/TLC，但如果需要严格分别限制 SLC 和 TLC 容量，建议后续增加独立容量参数。
