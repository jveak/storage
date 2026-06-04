import threading
import time
from datetime import datetime
from pathlib import Path

from kv_cache.backends import NVMeBackend
from kv_cache.benchmark import IntegratedBenchmark
from kv_cache.cache import MultiTierCache
from kv_cache.config import ConfigLoader, set_config
from kv_cache.models import GenerationMode, InferenceRequest, InferencePhase, MODEL_CONFIGS, QoSLevel
from kv_cache.prefix_cache import PrefixCacheEntry, PrefixCacheManager, PrefixType
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


def test_prefix_cache_first_match_warms_then_second_match_hits(tmp_path, monkeypatch):
    cache = MultiTierCache(
        model_config=MODEL_CONFIGS["tiny-1b"],
        gpu_memory_gb=0,
        cpu_memory_gb=0,
        cache_dir=str(tmp_path / "tlc"),
        storage_capacity_gb=0.01,
        seed=42,
    )
    manager = PrefixCacheManager(cache)
    prefix_entry = PrefixCacheEntry(
        prefix_key="system_fixed",
        prefix_type=PrefixType.SYSTEM_PROMPT,
        text_hash="fixed",
        token_count=5,
        kv_cache_key="kv_system_fixed",
    )
    monkeypatch.setattr(manager.prefix_matcher, "detect_system_prompt", lambda _tokens: prefix_entry)

    request = InferenceRequest(
        user_id="user_1",
        request_id="req_1",
        timestamp=datetime.now(),
        context_tokens=100,
        generate_tokens=10,
        priority=3,
        phase=InferencePhase.PREFILL_DECODE,
        qos_level=QoSLevel.INTERACTIVE,
    )

    entry, remaining_tokens, cache_type = manager.check_prefix_cache(request, MODEL_CONFIGS["tiny-1b"])
    assert entry is None
    assert remaining_tokens == request.context_tokens
    assert cache_type is None
    assert manager.stats["prefix_misses"] == 1
    assert manager.stats["prefix_hits"] == 0
    assert cache.contains(prefix_entry.kv_cache_key)

    entry, remaining_tokens, cache_type = manager.check_prefix_cache(request, MODEL_CONFIGS["tiny-1b"])
    assert entry is prefix_entry
    assert remaining_tokens == request.context_tokens - prefix_entry.token_count
    assert cache_type == "system"

    location, _ = cache.access_cache(entry.kv_cache_key, InferencePhase.DECODE, cache_type)
    assert location is not None
    manager.record_prefix_hit(entry, MODEL_CONFIGS["tiny-1b"])

    assert manager.stats["prefix_hits"] == 1
    assert manager.stats["system_prompt_reuse"] == 1
    assert manager.stats["bytes_saved"] == prefix_entry.token_count * MODEL_CONFIGS["tiny-1b"].kv_cache_size_per_token


def test_preconditioning_writes_directly_to_disk_not_cache_metadata(tmp_path):
    benchmark = IntegratedBenchmark(
        model_config=MODEL_CONFIGS["tiny-1b"],
        num_users=1,
        gpu_memory_gb=0,
        cpu_memory_gb=1,
        duration_seconds=1,
        cache_dir=str(tmp_path / "tlc"),
        generation_mode=GenerationMode.NONE,
        precondition=True,
        precondition_size_gb=0.001,
        precondition_threads=1,
    )

    benchmark._run_preconditioning()

    npy_files = list((tmp_path / "tlc").glob("precond_*.npy"))
    assert npy_files
    assert all(key not in benchmark.cache.cache_entries for key in ("precond_0", "precond_1"))
    stats = benchmark.cache.get_stats(duration=1)
    assert stats["write_operations"] == 0
    assert stats["storage_entries"] == 0


def test_storage_health_is_not_pass_when_only_cpu_tier_is_used(tmp_path):
    cache = MultiTierCache(
        model_config=MODEL_CONFIGS["tiny-1b"],
        gpu_memory_gb=0,
        cpu_memory_gb=1,
        cache_dir=str(tmp_path / "tlc"),
        seed=42,
    )

    success, tier, _ = cache.allocate_cache("cpu_only", num_tokens=10)
    assert success
    assert tier == "cpu"
    cache.access_cache("cpu_only", InferencePhase.DECODE)

    stats = cache.get_stats(duration=2)
    assert stats["storage_health"]["overall_status"] == "NOT_APPLICABLE"
    assert stats["read_operations"] == 1
    assert stats["write_operations"] == 1
    assert stats["read_iops"] == 0.5
    assert stats["write_iops"] == 0.5
    assert stats["storage_read_iops"] == 0
    assert stats["storage_write_iops"] == 0


def test_nvme_backend_preserves_existing_files_unless_force_clean(tmp_path):
    existing = tmp_path / "keep.npy"
    existing.write_bytes(b"existing")

    backend = NVMeBackend(base_path=str(tmp_path))
    assert existing.exists()
    backend.clear()
    existing.write_bytes(b"existing")

    NVMeBackend(base_path=str(tmp_path), clear_on_init=True)
    assert not existing.exists()


def test_synthetic_autoscaling_resizes_user_generators(tmp_path):
    benchmark = IntegratedBenchmark(
        model_config=MODEL_CONFIGS["tiny-1b"],
        num_users=1,
        gpu_memory_gb=0,
        cpu_memory_gb=0,
        duration_seconds=1,
        cache_dir=str(tmp_path / "tlc"),
        generation_mode=GenerationMode.NONE,
    )
    users = UserSimulator.generate_mixed_users(1)
    stop_event = threading.Event()
    thread = threading.Thread(target=benchmark.generate_requests, args=(users, stop_event))
    thread.start()

    deadline = time.time() + 2
    while benchmark._active_user_count() < 1 and time.time() < deadline:
        time.sleep(0.05)
    assert benchmark._active_user_count() == 1

    benchmark.num_users = 3
    deadline = time.time() + 2
    while benchmark._active_user_count() < 3 and time.time() < deadline:
        time.sleep(0.05)
    assert benchmark._active_user_count() == 3

    benchmark.num_users = 1
    deadline = time.time() + 2
    while benchmark._active_user_count() > 1 and time.time() < deadline:
        time.sleep(0.05)
    assert benchmark._active_user_count() == 1

    stop_event.set()
    thread.join(timeout=2)
    assert not thread.is_alive()
