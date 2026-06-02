import threading
import time
from datetime import datetime
from pathlib import Path

from kv_cache.benchmark import IntegratedBenchmark
from kv_cache.cache import MultiTierCache
from kv_cache.config import ConfigLoader, set_config
from kv_cache.models import GenerationMode, InferenceRequest, InferencePhase, MODEL_CONFIGS, QoSLevel
from kv_cache.workload import UserSimulator


CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.yaml"


def test_story_one_config_generates_only_interactive_users():
    loader = ConfigLoader(str(CONFIG_PATH))
    set_config(loader)
    try:
        users = UserSimulator.generate_mixed_users(50)
    finally:
        set_config(None)

    assert {user.qos_level for user in users} == {QoSLevel.INTERACTIVE}
    assert all(4096 <= user.context_length <= 8192 for user in users)
    assert all(128 <= user.generation_length <= 256 for user in users)


def test_storage_cache_is_lru_tier_before_disk(tmp_path):
    slc_dir = tmp_path / "slc"
    tlc_dir = tmp_path / "tlc"
    cache = MultiTierCache(
        model_config=MODEL_CONFIGS["tiny-1b"],
        gpu_memory_gb=0,
        cpu_memory_gb=0,
        cache_dir=str(tlc_dir),
        storage_cache_dir=str(slc_dir),
        storage_capacity_gb=0.001,
        seed=42,
    )

    for idx in range(6):
        success, tier, _ = cache.allocate_cache(f"entry_{idx}", num_tokens=10)
        assert success
        assert tier == "storage_cache"

    stats = cache.get_stats(duration=1)
    assert stats["storage_cache_entries"] > 0
    assert stats["disk_entries"] > 0
    assert stats["offloads_storage_cache"] > 0
    assert stats["offloads_disk"] > 0


def test_multi_turn_processing_waits_for_previous_turn(tmp_path):
    benchmark = IntegratedBenchmark(
        model_config=MODEL_CONFIGS["tiny-1b"],
        num_users=1,
        gpu_memory_gb=0,
        cpu_memory_gb=0,
        duration_seconds=1,
        cache_dir=str(tmp_path / "tlc"),
        generation_mode=GenerationMode.NONE,
    )

    stop_event = threading.Event()
    turn_1 = InferenceRequest(
        user_id="user_1",
        request_id="req_1",
        timestamp=datetime.now(),
        context_tokens=4096,
        generate_tokens=128,
        priority=3,
        phase=InferencePhase.PREFILL_DECODE,
        qos_level=QoSLevel.INTERACTIVE,
        conversation_id="conv_1",
        turn_number=1,
    )
    turn_2 = InferenceRequest(
        user_id="user_1",
        request_id="req_2",
        timestamp=datetime.now(),
        context_tokens=4096,
        generate_tokens=128,
        priority=3,
        phase=InferencePhase.PREFILL_DECODE,
        qos_level=QoSLevel.INTERACTIVE,
        conversation_id="conv_1",
        turn_number=2,
    )

    released = threading.Event()

    def waiter():
        if benchmark._wait_for_prior_turn(turn_2, stop_event):
            released.set()

    thread = threading.Thread(target=waiter)
    thread.start()
    time.sleep(0.2)
    assert not released.is_set()

    benchmark._mark_turn_complete(turn_1)
    thread.join(timeout=2)
    assert released.is_set()


def test_prefill_mode_records_cache_miss_recompute_latency(tmp_path):
    benchmark = IntegratedBenchmark(
        model_config=MODEL_CONFIGS["tiny-1b"],
        num_users=1,
        gpu_memory_gb=0,
        cpu_memory_gb=0,
        duration_seconds=1,
        cache_dir=str(tmp_path / "tlc"),
        generation_mode=GenerationMode.NONE,
        prefill_mode=GenerationMode.FAST,
    )
    request = InferenceRequest(
        user_id="user_1",
        request_id="req_1",
        timestamp=datetime.now(),
        context_tokens=2,
        generate_tokens=1,
        priority=3,
        phase=InferencePhase.DECODE,
        qos_level=QoSLevel.INTERACTIVE,
    )

    latency = benchmark._simulate_cache_miss_prefill(request)

    assert abs(latency - 0.001) < 1e-9
    assert benchmark.results["cache_miss_prefill_compute_latencies"] == [latency]
    assert benchmark.results["total_cache_miss_prefill_compute_latency"] == latency
