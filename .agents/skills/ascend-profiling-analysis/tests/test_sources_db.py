"""Unit tests for the db-direct input source (``ascend_profile.sources_db``).

The fixture is a synthetic sqlite db built on the fly (never committed):
a handful of compute kernels (vector / mix / cube-core), one MIX kernel
with the two-TASK-row layout, one hcom comm op plus its AicpuKernel
companion, NULL shape fields, a compute row without PMU metrics, and rows
inserted out of start-time order.  It exercises the field mapping, the
row-union rule, the idle-gap Wait Time rule, the schema probe's
fail-closed behaviour, and the normalize ``--source`` wiring end to end.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import conftest  # noqa: F401
import pytest

from ascend_profile import sources_db
from ascend_profile.normalize import normalize_profile
from ascend_profile.sources_db import (
    db_row_reader,
    iter_db_rows,
    iter_kernel_events_from_db,
    load_db_mapping,
    probe_db_schema,
)

INVALID_ID = 4294967295

# STRING_IDS id assignments (arbitrary but stable within the fixture).
SID = {
    "aclnnFill_FillAiCore_Fill": 100,
    "aclnnMatmul_Matmul": 101,
    "aclnnRope_Rope": 102,
    "aclnnNorm_Norm": 103,
    "Fill": 110,
    "Matmul": 111,
    "Rope": 112,
    "Norm": 113,
    "AI_VECTOR_CORE": 120,
    "MIX_AIC": 121,
    "AI_CORE": 122,
    "AI_CPU": 123,
    "KERNEL_AIVEC": 130,
    "KERNEL_MIX_AIC": 131,
    "KERNEL_AICORE": 132,
    "KERNEL_AICPU": 133,
    "dynamic": 140,
    "static": 141,
    "NO": 142,
    "YES": 143,
    '"1;"': 150,
    "ND": 151,
    "INT32": 152,
    '"4,8"': 153,
    "ND;ND": 154,
    "DT_BF16;FLOAT": 155,
    '"16"': 156,
    "hcom_allReduce__1_0_1": 160,
    "hcom_allReduce_": 161,
    "allreduceAicpuKernel": 162,
}

PMU_METRIC_IDS = {}
for _idx, _name in enumerate((
    "aic_total_time", "aic_total_cycles", "aic_mac_time", "aic_mac_ratio",
    "aic_scalar_time", "aic_scalar_ratio", "aic_mte1_time", "aic_mte1_ratio",
    "aic_mte2_time", "aic_mte2_ratio", "aic_fixpipe_time", "aic_fixpipe_ratio",
    "aic_icache_miss_rate", "aiv_total_time", "aiv_total_cycles",
    "aiv_vec_time", "aiv_vec_ratio", "aiv_scalar_time", "aiv_scalar_ratio",
    "aiv_mte2_time", "aiv_mte2_ratio", "aiv_mte3_time", "aiv_mte3_ratio",
    "aiv_icache_miss_rate",
    # present in real dbs but intentionally not mapped to any CSV column:
    "aiv_mac_time", "aiv_mac_ratio", "aiv_mte1_time", "aiv_mte1_ratio",
)):
    PMU_METRIC_IDS[_name] = 200 + _idx


def _sid(name: str) -> int:
    return SID[name]


def _pmu_id(name: str) -> int:
    return PMU_METRIC_IDS[name]


def _create_schema(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        CREATE TABLE TASK(startNs INTEGER, endNs INTEGER, deviceId INTEGER, connectionId INTEGER,
                          globalTaskId INTEGER, globalPid INTEGER, taskType INTEGER, contextId INTEGER,
                          streamId INTEGER, taskId INTEGER, modelId INTEGER);
        CREATE TABLE COMPUTE_TASK_INFO(name INTEGER, globalTaskId INTEGER, blockNum INTEGER,
                          mixBlockNum INTEGER, taskType INTEGER, opType INTEGER, inputFormats INTEGER,
                          inputDataTypes INTEGER, inputShapes INTEGER, outputFormats INTEGER,
                          outputDataTypes INTEGER, outputShapes INTEGER, attrInfo INTEGER,
                          opState INTEGER, hf32Eligible INTEGER, gridDim INTEGER, blockDim INTEGER);
        CREATE TABLE TASK_PMU_INFO(globalTaskId INTEGER, name INTEGER, value NUMERIC);
        CREATE TABLE STRING_IDS(id INTEGER, value TEXT);
        CREATE TABLE COMMUNICATION_OP(opName INTEGER, startNs INTEGER, endNs INTEGER, connectionId INTEGER,
                          groupName INTEGER, opId INTEGER, relay INTEGER, retry INTEGER, dataType INTEGER,
                          algType INTEGER, count NUMERIC, opType INTEGER, deviceId INTEGER, rankSize INTEGER);
        CREATE TABLE COMMUNICATION_SCHEDULE_TASK_INFO(name INTEGER, globalTaskId INTEGER,
                          taskType INTEGER, opType INTEGER);
        """
    )


def _insert_task(con, *, start, end, gtid, task_type, stream, task_id, context=INVALID_ID, device=0, model=INVALID_ID):
    con.execute(
        "INSERT INTO TASK VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (start, end, device, 1, gtid, 100, _sid(task_type), context, stream, task_id, model),
    )


def _insert_cti(con, *, name, gtid, block, mix, core, op_type, shapes, op_state="dynamic", hf32="NO"):
    shape_ids = [None if value is None else _sid(value) for value in shapes]
    con.execute(
        "INSERT INTO COMPUTE_TASK_INFO VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            _sid(name), gtid, block, mix, _sid(core), _sid(op_type),
            shape_ids[2], shape_ids[1], shape_ids[0], shape_ids[5], shape_ids[4], shape_ids[3],
            0, _sid(op_state), _sid(hf32), 0, 0,
        ),
    )


def _insert_pmu(con, cti_rowid: int, values: dict[str, float]) -> None:
    for metric, value in values.items():
        con.execute("INSERT INTO TASK_PMU_INFO VALUES (?,?,?)", (cti_rowid, _pmu_id(metric), value))


def build_fixture_db(path: Path) -> None:
    """Five emitted rows, inserted deliberately out of start-time order.

    Timeline (ns): aicpu stream-10 kernel is inserted first although its
    start (1_500_000) sorts fourth — proves the emitted stream is sorted by
    Start Time, not insertion order.
    """

    con = sqlite3.connect(path)
    _create_schema(con)
    for name, str_id in list(SID.items()) + list(PMU_METRIC_IDS.items()):
        con.execute("INSERT INTO STRING_IDS VALUES (?,?)", (str_id, name))

    # --- aicpu comm kernel on stream 10 (exported via COMM_SCHED + TASK) ---
    _insert_task(con, start=1_500_000, end=1_700_000, gtid=40, task_type="KERNEL_AICPU", stream=10, task_id=99)
    con.execute(
        "INSERT INTO COMMUNICATION_SCHEDULE_TASK_INFO VALUES (?,?,?,?)",
        (_sid("allreduceAicpuKernel"), 40, _sid("AI_CPU"), _sid("allreduceAicpuKernel")),
    )

    # --- vector kernel, first task on stream 47, full PMU set (CTI rowid 1) ---
    _insert_task(con, start=1_000_000, end=1_001_520, gtid=10, task_type="KERNEL_AIVEC", stream=47, task_id=7)
    _insert_cti(
        con, name="aclnnFill_FillAiCore_Fill", gtid=10, block=1, mix=0,
        core="AI_VECTOR_CORE", op_type="Fill",
        shapes=('"1;"', "INT32", "ND", '"4,8"', "INT32", "ND"),
    )
    _insert_pmu(con, 1, {
        "aiv_total_time": 970.909090909091, "aiv_total_cycles": 1602,
        "aiv_vec_time": 34.54545454545455, "aiv_vec_ratio": 0.035580524344569285,
        "aiv_scalar_time": 386.0606060606061, "aiv_scalar_ratio": 0.3976279650436954,
        "aiv_mte2_time": 109.09090909090908, "aiv_mte2_ratio": 0.11235955056179775,
        "aiv_mte3_time": 92.72727272727273, "aiv_mte3_ratio": 0.09550561797752809,
        "aiv_icache_miss_rate": 0.1904761904761905,
        # unmapped metrics must be dropped silently:
        "aiv_mac_time": 999.0, "aiv_mte1_time": 888.0,
    })
    # unknown metric id without a STRING_IDS entry: dropped silently too.
    con.execute("INSERT INTO TASK_PMU_INFO VALUES (?,?,?)", (1, 9999, 1.0))

    # --- MIX_AIC kernel: two TASK rows, only the CTI-referenced part exported ---
    # part A (not referenced by any CTI row): overlapping, longer, ends later.
    _insert_task(con, start=1_002_760, end=1_103_880, gtid=20, task_type="KERNEL_MIX_AIC", stream=47, task_id=8, context=0)
    # part B (exported; CTI rowid 2)
    _insert_task(con, start=1_004_000, end=1_102_700, gtid=21, task_type="KERNEL_MIX_AIC", stream=47, task_id=8, context=0)
    _insert_cti(
        con, name="aclnnMatmul_Matmul", gtid=21, block=20, mix=20,
        core="MIX_AIC", op_type="Matmul",
        shapes=('"4,8"', "DT_BF16;FLOAT", "ND;ND", '"16"', "INT32", "ND"),
        op_state="static", hf32="YES",
    )
    _insert_pmu(con, 2, {
        "aic_total_time": 84305.48484848, "aic_total_cycles": 2782081,
        "aic_mac_time": 59304.1212, "aic_mac_ratio": 0.7034,
        "aic_mte2_ratio": 0.5698,
    })

    # --- hcom comm op (no TASK row; exported from COMMUNICATION_OP) ---
    con.execute(
        "INSERT INTO COMMUNICATION_OP VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (_sid("hcom_allReduce__1_0_1"), 1_200_000, 2_445_640, 5, 0, 0, 0, 0, 0, 0, 1024, _sid("hcom_allReduce_"), 0, 8),
    )

    # --- cube-core kernel on stream 48 with NULL shapes (CTI rowid 3) ---
    _insert_task(con, start=2_000_000, end=2_010_000, gtid=30, task_type="KERNEL_AICORE", stream=48, task_id=3)
    _insert_cti(
        con, name="aclnnRope_Rope", gtid=30, block=8, mix=0,
        core="AI_CORE", op_type="Rope",
        shapes=(None, None, None, None, None, None),
    )
    # Pins the cube_utilization formula on a block != total_cores case:
    # 14409.62ns * 8 blocks / (10000ns * 20 cores) * 100 = 57.638%.
    _insert_pmu(con, 3, {"aic_total_time": 14409.62})

    # --- second cube-core kernel on stream 48, NO PMU rows at all (rowid 4) ---
    _insert_task(con, start=3_000_000, end=3_010_000, gtid=31, task_type="KERNEL_AICORE", stream=48, task_id=4)
    _insert_cti(
        con, name="aclnnNorm_Norm", gtid=31, block=8, mix=0,
        core="AI_CORE", op_type="Norm",
        shapes=('"4,8"', "DT_BF16;FLOAT", "ND;ND", '"4,8"', "DT_BF16;FLOAT", "ND;ND"),
    )

    con.commit()
    con.close()


@pytest.fixture()
def fixture_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "ascend_pytorch_profiler_0.db"
    build_fixture_db(db_path)
    return db_path


def _rows_by_name(db_path: Path) -> dict[str, dict[str, str]]:
    return {row["Name"]: row for _idx, row in iter_kernel_events_from_db(db_path)}


# ----------------------------------------------------------------------------
# mapping knowledge file
# ----------------------------------------------------------------------------


def test_shipped_mapping_file_is_valid() -> None:
    mapping = load_db_mapping()
    assert len(mapping["csv_header"]) == 46
    assert len(mapping["pmu_defs"]) == 24
    assert set(mapping["required_tables"]) == {
        "TASK", "COMPUTE_TASK_INFO", "TASK_PMU_INFO", "STRING_IDS",
    }
    # TP1 captures (no HCCL) omit the comm tables entirely from the db, so
    # they must stay optional (verified 2026-09-03 on Qwen3-8B TP1).
    assert set(mapping["optional_tables"]) == {
        "COMMUNICATION_OP", "COMMUNICATION_SCHEDULE_TASK_INFO",
    }
    assert mapping["invalid_id"] == INVALID_ID
    assert mapping["documented_differences"]


# ----------------------------------------------------------------------------
# schema probe
# ----------------------------------------------------------------------------


def test_probe_db_schema_ok(fixture_db: Path) -> None:
    probe = probe_db_schema(fixture_db)
    assert probe["ok"], probe
    assert probe["missing"] == {}
    assert probe["error"] is None


def test_probe_db_schema_missing_table(tmp_path: Path) -> None:
    db_path = tmp_path / "broken.db"
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE STRING_IDS(id INTEGER, value TEXT)")
    con.commit()
    con.close()
    probe = probe_db_schema(db_path)
    assert not probe["ok"]
    assert "TASK" in probe["missing"]
    assert "TASK_PMU_INFO" in probe["missing"]


def test_probe_db_schema_missing_column(tmp_path: Path) -> None:
    db_path = tmp_path / "broken.db"
    con = sqlite3.connect(db_path)
    _create_schema(con)
    con.execute("ALTER TABLE TASK DROP COLUMN contextId")
    con.commit()
    con.close()
    probe = probe_db_schema(db_path)
    assert not probe["ok"]
    assert probe["missing"] == {"TASK": ["contextId"]}


def test_probe_db_schema_missing_file(tmp_path: Path) -> None:
    probe = probe_db_schema(tmp_path / "absent.db")
    assert not probe["ok"]
    assert probe["error"]


def test_probe_db_schema_comm_tables_optional(tmp_path: Path) -> None:
    """TP1 captures have no HCCL: the COMMUNICATION_* tables are absent from
    the db entirely. The probe must pass and report them as optional-missing
    (zero comm rows), never fail closed."""
    db_path = tmp_path / "tp1.db"
    con = sqlite3.connect(db_path)
    con.executescript(
        """
        CREATE TABLE TASK(startNs INTEGER, endNs INTEGER, deviceId INTEGER, connectionId INTEGER,
                          globalTaskId INTEGER, globalPid INTEGER, taskType INTEGER, contextId INTEGER,
                          streamId INTEGER, taskId INTEGER, modelId INTEGER);
        CREATE TABLE COMPUTE_TASK_INFO(name INTEGER, globalTaskId INTEGER, blockNum INTEGER,
                          mixBlockNum INTEGER, taskType INTEGER, opType INTEGER, inputFormats INTEGER,
                          inputDataTypes INTEGER, inputShapes INTEGER, outputFormats INTEGER,
                          outputDataTypes INTEGER, outputShapes INTEGER, attrInfo INTEGER,
                          opState INTEGER, hf32Eligible INTEGER, gridDim INTEGER, blockDim INTEGER);
        CREATE TABLE TASK_PMU_INFO(globalTaskId INTEGER, name INTEGER, value NUMERIC);
        CREATE TABLE STRING_IDS(id INTEGER, value TEXT);
        """
    )
    con.commit()
    con.close()
    probe = probe_db_schema(db_path)
    assert probe["ok"], probe
    assert probe["missing"] == {}
    assert set(probe["optional_missing"]) == {"COMMUNICATION_OP", "COMMUNICATION_SCHEDULE_TASK_INFO"}


# ----------------------------------------------------------------------------
# row reconstruction
# ----------------------------------------------------------------------------


def test_rows_without_comm_tables(tmp_path: Path) -> None:
    """A db without COMMUNICATION_* tables (TP1, no HCCL) still yields its
    compute rows; zero comm rows are contributed instead of an error."""
    db_path = tmp_path / "ascend_pytorch_profiler_0.db"
    con = sqlite3.connect(db_path)
    con.executescript(
        """
        CREATE TABLE TASK(startNs INTEGER, endNs INTEGER, deviceId INTEGER, connectionId INTEGER,
                          globalTaskId INTEGER, globalPid INTEGER, taskType INTEGER, contextId INTEGER,
                          streamId INTEGER, taskId INTEGER, modelId INTEGER);
        CREATE TABLE COMPUTE_TASK_INFO(name INTEGER, globalTaskId INTEGER, blockNum INTEGER,
                          mixBlockNum INTEGER, taskType INTEGER, opType INTEGER, inputFormats INTEGER,
                          inputDataTypes INTEGER, inputShapes INTEGER, outputFormats INTEGER,
                          outputDataTypes INTEGER, outputShapes INTEGER, attrInfo INTEGER,
                          opState INTEGER, hf32Eligible INTEGER, gridDim INTEGER, blockDim INTEGER);
        CREATE TABLE TASK_PMU_INFO(globalTaskId INTEGER, name INTEGER, value NUMERIC);
        CREATE TABLE STRING_IDS(id INTEGER, value TEXT);
        """
    )
    for name, str_id in list(SID.items()) + list(PMU_METRIC_IDS.items()):
        con.execute("INSERT INTO STRING_IDS VALUES (?,?)", (str_id, name))
    _insert_task(con, start=1_000_000, end=1_001_520, gtid=10, task_type="KERNEL_AIVEC", stream=47, task_id=7)
    _insert_cti(
        con, name="aclnnFill_FillAiCore_Fill", gtid=10, block=1, mix=0,
        core="AI_VECTOR_CORE", op_type="Fill",
        shapes=('"1;"', "INT32", "ND", '"4,8"', "INT32", "ND"),
    )
    con.commit()
    con.close()
    rows = list(iter_kernel_events_from_db(db_path))
    assert len(rows) == 1
    assert rows[0][1]["Name"] == "aclnnFill_FillAiCore_Fill"
    assert rows[0][1]["Accelerator Core"] == "AI_VECTOR_CORE"


def test_row_union_and_start_time_order(fixture_db: Path) -> None:
    rows = list(iter_db_rows(fixture_db))
    # 4 compute + 1 hcom + 1 aicpu; the non-exported MIX part A adds nothing.
    assert len(rows) == 6
    names = [dict(zip(load_db_mapping()["csv_header"], cells))["Name"] for _idx, cells in rows]
    assert names == [
        "aclnnFill_FillAiCore_Fill",   # start 1000.000
        "aclnnMatmul_Matmul",          # start 1004.000
        "hcom_allReduce__1_0_1",       # start 1200.000
        "allreduceAicpuKernel",        # start 1500.000 (inserted first)
        "aclnnRope_Rope",              # start 2000.000
        "aclnnNorm_Norm",              # start 3000.000
    ]


def test_compute_row_exact_cells(fixture_db: Path) -> None:
    rows = _rows_by_name(fixture_db)
    fill = rows["aclnnFill_FillAiCore_Fill"]
    assert fill["Device_id"] == "0"
    assert fill["Model ID"] == str(INVALID_ID)
    assert fill["Task ID"] == "7"
    assert fill["Stream ID"] == "47"
    assert fill["Type"] == "Fill"
    assert fill["OP State"] == "dynamic"
    assert fill["Accelerator Core"] == "AI_VECTOR_CORE"
    assert fill["Start Time(us)"] == "1000.000"
    assert not fill["Start Time(us)"].endswith("\t")
    assert fill["Duration(us)"] == "1.520"
    assert fill["Wait Time(us)"] == "0.000000"  # first task on stream 47
    assert fill["Block Num"] == "1"
    assert fill["Mix Block Num"] == "0"
    assert fill["HF32 Eligible"] == "NO"
    assert fill["Input Shapes"] == '"1;"'
    assert fill["Input Data Types"] == "INT32"
    assert fill["Input Formats"] == "ND"
    assert fill["Output Shapes"] == '"4,8"'
    assert fill["Context ID"] == "N/A"
    # PMU pivot: scaled floats round to 3 decimals, cycles print as int.
    assert fill["aicore_time(us)"] == "0.0"
    assert fill["aic_total_cycles"] == "0"
    assert fill["aiv_time(us)"] == "0.971"
    assert fill["aiv_total_cycles"] == "1602"
    assert fill["aiv_vec_time(us)"] == "0.035"
    assert fill["aiv_vec_ratio"] == "0.036"
    assert fill["aiv_scalar_ratio"] == "0.398"
    assert fill["aiv_mte2_ratio"] == "0.112"
    assert fill["aiv_mte3_time(us)"] == "0.093"
    assert fill["aiv_icache_miss_rate"] == "0.19"
    assert fill["cube_utilization(%)"] == "0.000"


def test_mix_kernel_exports_only_cti_part(fixture_db: Path) -> None:
    rows = _rows_by_name(fixture_db)
    mix = rows["aclnnMatmul_Matmul"]
    # Start/Duration come from part B (the CTI-referenced TASK row), never
    # from the overlapping part A.
    assert mix["Start Time(us)"] == "1004.000"
    assert mix["Duration(us)"] == "98.700"
    assert mix["Accelerator Core"] == "MIX_AIC"
    assert mix["OP State"] == "static"
    assert mix["HF32 Eligible"] == "YES"
    assert mix["Block Num"] == "20"
    assert mix["Mix Block Num"] == "20"
    assert mix["Context ID"] == "0"
    # Wait = gap to the previous *exported* end on stream 47 (fill kernel):
    # (1_004_000 - 1_001_520) / 1000 = 2.48us.
    assert mix["Wait Time(us)"] == "2.480000"
    assert mix["aicore_time(us)"] == "84.305"
    assert mix["aic_total_cycles"] == "2782081"
    assert mix["aic_mac_ratio"] == "0.703"
    assert mix["aic_mte2_ratio"] == "0.57"  # trailing zeros trimmed by str(round())
    # cube_utilization = aic_total_time / duration * 100
    assert mix["cube_utilization(%)"] == "85.416"


def test_hcom_row_constants_and_device_wide_wait(fixture_db: Path) -> None:
    rows = _rows_by_name(fixture_db)
    hcom = rows["hcom_allReduce__1_0_1"]
    assert hcom["Type"] == "hcom_allReduce_"
    assert hcom["Accelerator Core"] == "COMMUNICATION"
    assert hcom["Task ID"] == "N/A"
    assert hcom["Stream ID"] == "N/A"
    assert hcom["Model ID"] == str(INVALID_ID)
    assert hcom["OP State"] == "N/A"
    assert hcom["HF32 Eligible"] == "N/A"
    assert hcom["Block Num"] == "0"
    assert hcom["Mix Block Num"] == "N/A"
    assert hcom["Input Shapes"] == "N/A"
    assert hcom["Context ID"] == "N/A"
    assert hcom["Start Time(us)"] == "1200.000"
    assert hcom["Duration(us)"] == "1245.640"
    # Device-wide idle gap: previous exported end is the MIX part B end
    # (1_102_700); part A's later end (1_103_880) is NOT exported and must
    # not anchor the gap.  (1_200_000 - 1_102_700) / 1000 = 97.3us.
    assert hcom["Wait Time(us)"] == "97.300000"
    assert hcom["aicore_time(us)"] == "N/A"
    assert hcom["cube_utilization(%)"] == "N/A"


def test_aicpu_row_constants(fixture_db: Path) -> None:
    rows = _rows_by_name(fixture_db)
    aicpu = rows["allreduceAicpuKernel"]
    assert aicpu["Type"] == "allreduceAicpuKernel"
    assert aicpu["Accelerator Core"] == "AI_CPU"
    assert aicpu["OP State"] == "dynamic"
    assert aicpu["HF32 Eligible"] == "NO"
    # Not present anywhere in the db schema (documented adoption).
    assert aicpu["Block Num"] == "N/A"
    assert aicpu["Mix Block Num"] == "N/A"
    assert aicpu["Task ID"] == "99"
    assert aicpu["Stream ID"] == "10"
    assert aicpu["Input Shapes"] == "N/A"
    assert aicpu["Context ID"] == "N/A"
    assert aicpu["Start Time(us)"] == "1500.000"
    assert aicpu["Duration(us)"] == "200.000"
    assert aicpu["Wait Time(us)"] == "0.000000"  # first task on stream 10
    assert aicpu["aiv_time(us)"] == "N/A"


def test_compute_row_null_shapes_and_cube_utilization(fixture_db: Path) -> None:
    rows = _rows_by_name(fixture_db)
    rope = rows["aclnnRope_Rope"]
    assert rope["Accelerator Core"] == "AI_CORE"
    assert rope["Input Shapes"] == "N/A"
    assert rope["Output Formats"] == "N/A"
    assert rope["Wait Time(us)"] == "0.000000"  # first task on stream 48
    # cube_utilization = aic_total_time * blockNum / (duration * 20 cores):
    # 14409.62 * 8 / (10000 * 20) * 100 = 57.63848 -> 57.638
    assert rope["cube_utilization(%)"] == "57.638"
    assert rope["aicore_time(us)"] == "14.41"


def test_compute_row_without_pmu_emits_zeros(fixture_db: Path) -> None:
    rows = _rows_by_name(fixture_db)
    norm = rows["aclnnNorm_Norm"]
    assert norm["Accelerator Core"] == "AI_CORE"
    assert norm["Input Shapes"] == '"4,8"'
    assert norm["aicore_time(us)"] == "0.0"
    assert norm["aiv_total_cycles"] == "0"
    assert norm["cube_utilization(%)"] == "0.000"
    # second task on stream 48: gap to the rope kernel's end
    assert norm["Wait Time(us)"] == "990.000000"


def test_dict_api_matches_csv_header(fixture_db: Path) -> None:
    mapping = load_db_mapping()
    for row_idx, row in iter_kernel_events_from_db(fixture_db):
        assert tuple(row.keys()) == mapping["csv_header"]
        assert set(row) == set(mapping["csv_header"])
    assert row_idx == 5


def test_db_row_reader_accessor_integration(fixture_db: Path) -> None:
    accessor, rows = db_row_reader(fixture_db)
    rows = list(rows)
    first = rows[0][1]
    assert accessor.name(first) == "aclnnFill_FillAiCore_Fill"
    assert accessor.task_type(first) == "FILL"
    assert accessor.core(first) == "AI_VECTOR_CORE"
    assert accessor.stream(first) == "47"
    start, end, duration, wait = accessor.event_time(first)
    assert (start, end, duration, wait) == (1000.0, 1001.52, 1.52, 0.0)
    pipeline = accessor.pipeline_breakdown(first)
    assert pipeline["aiv_time"] == pytest.approx(0.971)
    assert pipeline["aiv_vec_time"] == pytest.approx(0.035)
    mix = rows[1][1]
    assert accessor.core(mix) == "MIX_AIC"
    assert accessor.pipeline_breakdown(mix)["aicore_time"] == pytest.approx(84.305)
    hcom = rows[2][1]
    assert accessor.name(hcom) == "hcom_allReduce__1_0_1"
    assert accessor.core(hcom) == "COMMUNICATION"


# ----------------------------------------------------------------------------
# normalize --source wiring
# ----------------------------------------------------------------------------


def _make_rank_tree(tmp_path: Path) -> Path:
    rank_dir = tmp_path / "rank0_test_ascend_pt" / "ASCEND_PROFILER_OUTPUT"
    rank_dir.mkdir(parents=True)
    build_fixture_db(rank_dir / "ascend_pytorch_profiler_0.db")
    return tmp_path


def test_normalize_auto_prefers_db(tmp_path: Path) -> None:
    profile_root = _make_rank_tree(tmp_path)
    events, manifest = normalize_profile(profile_root, tmp_path / "out")
    assert manifest["rank_count"] == 1
    assert manifest["event_count"] == 6
    assert manifest["source"] == "auto"
    assert manifest["source_kinds"] == {"rank0_test_ascend_pt": "kernel_details_db"}
    assert manifest["source_notes"] == []
    assert events[0].name_raw == "aclnnFill_FillAiCore_Fill"
    assert events[0].pipeline_us["aiv_time"] == pytest.approx(0.971)
    assert events[0].wait_us == 0.0
    assert events[1].accelerator_core == "MIX_AIC"
    assert events[1].wait_us == pytest.approx(2.48)
    assert events[2].name_raw == "hcom_allReduce__1_0_1"
    assert events[3].accelerator_core == "AI_CPU"
    source_index = (tmp_path / "out" / "source_index.json").read_text(encoding="utf-8")
    assert "kernel_details_db" in source_index


def test_normalize_csv_mode_skips_db_only_rank(tmp_path: Path) -> None:
    profile_root = _make_rank_tree(tmp_path)
    _events, manifest = normalize_profile(profile_root, tmp_path / "out", source="csv")
    assert manifest["rank_count"] == 0
    assert manifest["event_count"] == 0
    assert manifest["source_notes"]


def test_normalize_db_mode_uses_db(tmp_path: Path) -> None:
    profile_root = _make_rank_tree(tmp_path)
    events, manifest = normalize_profile(profile_root, tmp_path / "out", source="db")
    assert manifest["event_count"] == 6
    assert manifest["source_kinds"] == {"rank0_test_ascend_pt": "kernel_details_db"}
    assert [event.row_idx for event in events] == [0, 1, 2, 3, 4, 5]


def test_normalize_db_mode_rejects_broken_db(tmp_path: Path) -> None:
    rank_dir = tmp_path / "rank0_test_ascend_pt" / "ASCEND_PROFILER_OUTPUT"
    rank_dir.mkdir(parents=True)
    con = sqlite3.connect(rank_dir / "ascend_pytorch_profiler_0.db")
    con.execute("CREATE TABLE STRING_IDS(id INTEGER, value TEXT)")
    con.commit()
    con.close()
    _events, manifest = normalize_profile(tmp_path, tmp_path / "out", source="db")
    assert manifest["rank_count"] == 0
    assert "db schema probe failed" in manifest["source_notes"][0]
