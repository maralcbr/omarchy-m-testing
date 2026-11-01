require "test_helper"

# Seam B: benchmark scores upload with the report (the CLI's golden benchmark
# report, schema/golden/benchmarks/) and are compared per Mac model and
# Omarchy stack/version.
class BenchmarksTest < ActionDispatch::IntegrationTest
  M2 = "benchmarks/m2-max-converged"

  # The golden benchmark run with its scores scaled, optionally on another Mac (a corpus golden's machine and system).
  def run_with(factor, on: nil, suite: nil)
    golden(M2).tap do |report|
      if on
        other = golden(on)
        report["machine"] = other["machine"]
        report["system"] = other["system"]
      end
      report["checks"].each do |check|
        next unless check["score"]

        check["score"]["value"] = (check["score"]["value"] * factor).round
        check["score"]["suite"] = suite if suite
      end
    end
  end

  def opengl_rows = css_select("#benchmark-opengl-suite-1 tr.score-row")
  def storage_rows = css_select("#benchmark-storage-suite-1 tr.score-row")

  # A golden run with a storage score, on the given capacity (nil: a report from before capacity was recorded).
  def storage_run(value, capacity: "1TB", on: nil)
    run_with(1, on:).tap do |report|
      report["machine"]["storage"] = capacity if capacity
      report["checks"] << { "id" => "benchmark.storage", "kind" => "automatic", "status" => "pass", "evidence" => [ "sequential read: #{value} MB/s" ],
                            "classification" => { "outcome" => "works", "feature" => "nvme", "layer" => "asahi", "expected" => { "asahi" => "upstream" } },
                            "score" => { "value" => value, "unit" => "MBps", "tool" => "fio 3.38", "suite" => 1 } }
    end
  end

  def with_reference(board, document)
    Dir.mktmpdir do |dir|
      File.write(File.join(dir, "#{board}.json"), document.to_json)
      previous = ENV["MACOS_REFERENCE_PATH"]
      ENV["MACOS_REFERENCE_PATH"] = dir
      MacosReference.instance_variable_set(:@references, nil)
      yield
    ensure
      ENV["MACOS_REFERENCE_PATH"] = previous
      MacosReference.instance_variable_set(:@references, nil)
    end
  end

  REFERENCE = { "conditions" => { "power" => "ac", "brightness" => "about 50%", "displays" => 1 }, "macos" => { "version" => "26.6" }, "captured" => "2026-11-01",
                "metrics" => { "h264_decode_fps" => 900 }, "storage" => { "1TB" => { "read_mbps" => 6000 } } }.freeze

  test "storage rows split by capacity" do
    upload_report storage_run(3000, capacity: "1TB"), machine: "a"
    upload_report storage_run(6000, capacity: "2TB"), machine: "b"

    get "/benchmarks"

    assert_select "#benchmark-storage-suite-1 .card-title", "storage · suite 1"
    assert_equal [ "2TB", "1TB" ], storage_rows.map { |row| css_select(row, ".capacity-cell").text.strip }
    assert_select "#benchmark-storage-suite-1 tr.score-row .score-value", "6,000"
    assert_select "#benchmark-storage-suite-1 th", "MB/s"
  end

  test "a report without a capacity is accepted and shows as unknown" do
    upload_report storage_run(3000, capacity: nil), machine: "a"

    get "/benchmarks"

    assert_select "#benchmark-storage-suite-1 .capacity-cell", "unknown"
  end

  test "other benchmarks don't split by capacity" do
    upload_report storage_run(3000, capacity: "1TB"), machine: "a"
    upload_report storage_run(6000, capacity: "2TB"), machine: "b"

    get "/benchmarks"

    assert_equal 1, opengl_rows.size
    assert_select "#benchmark-opengl-suite-1 .capacity-cell", false
  end

  test "the macOS column shows the same board's number with its conditions" do
    with_reference("j416c", REFERENCE) do
      upload_report golden(M2), machine: "a"

      get "/benchmarks"

      assert_select "#benchmark-h264-decode-suite-1 .macos-cell", /900/
      assert_select "#benchmark-h264-decode-suite-1 .macos-cell", /macOS 26\.6, on AC, brightness about 50%, 1 display, 2026-11-01/
    end
  end

  test "GL and Vulkan have no macOS column" do
    with_reference("j416c", REFERENCE) do
      upload_report golden(M2), machine: "a"

      get "/benchmarks"

      assert_select "#benchmark-opengl-suite-1 th", { text: "macOS", count: 0 }
      assert_select "#benchmark-vulkan-suite-1 .macos-cell", false
    end
  end

  test "storage shows macOS only at the same capacity" do
    with_reference("j416c", REFERENCE) do
      upload_report storage_run(3000, capacity: "1TB"), machine: "a"
      upload_report storage_run(6000, capacity: "2TB"), machine: "b"

      get "/benchmarks"

      two, one = storage_rows
      assert_select one, ".macos-cell", /6,000/
      assert_select two, ".macos-cell", /no macOS reference at this capacity/
    end
  end

  test "a board with no reference shows a dash" do
    upload_report golden(M2), machine: "a"

    get "/benchmarks"

    assert_select "#benchmark-h264-decode-suite-1 .macos-cell", "–"
  end

  test "the export carries the capacity and the macOS number" do
    with_reference("j416c", REFERENCE) do
      upload_report storage_run(3000, capacity: "1TB"), machine: "a"

      get "/api/v1/benchmarks"

      row = JSON.parse(response.body)["benchmarks"].find { |benchmark| benchmark["check"] == "benchmark.storage" }["rows"].first
      assert_equal [ "1TB", 6000 ], row.values_at("capacity", "macos")
    end
  end

  test "the run page shows the SSD's capacity, and a report without it leaves it out" do
    with_capacity = upload_report storage_run(3000, capacity: "1TB"), machine: "a"
    without = upload_report golden(M2), machine: "b"

    get path_of(with_capacity["report_url"])
    assert_select "dd#storage", "1TB"
    get path_of(without["report_url"])
    assert_select "dd#storage", false
  end

  test "the golden benchmark report uploads with its scores" do
    body = upload_report golden(M2)

    assert_response :created
    stored = Report.find_by!(public_id: body["id"])
    assert_equal({ "value" => 2987, "unit" => "points", "tool" => "glmark2 2023.01", "suite" => 1 },
                 stored.checks.find { |check| check["id"] == "benchmark.opengl" }["score"])
  end

  test "each benchmark compares Macs, best first, each machine counted once with its latest score" do
    upload_report run_with(0.5), machine: "a"                        # an older run on the same M2: replaced by the next
    upload_report golden(M2), machine: "a"
    upload_report run_with(1.1), machine: "c"                        # a second M2 Max
    upload_report run_with(0.6, on: "m1-pro-converged"), machine: "b"

    get "/benchmarks"

    assert_response :success
    assert_select "#benchmark-opengl-suite-1 .card-title", "opengl · suite 1"
    assert_equal [ "j416c", "j314s" ], opengl_rows.map { |row| row["data-board"] }
    m2, m1 = opengl_rows
    # The M2's two machines: 2987 and 3286 (the median is between them); the older 1494 run doesn't count.
    assert_select m2, ".score-value", "3,136.5"
    assert_select m2, ".score-range", "2,987–3,286"
    assert_select m2, "td", /\A2 \(3 runs\)\z/
    assert_select m2, ".score-bar[style=?]", "--share: 100.0%"
    assert_select m1, ".score-value", "1,792"
    assert_select m1, ".score-bar[style=?]", "--share: 57.1%"
    assert_select m1, "td", "converged 4.0.0 · build 99ace40.361571887310001 (linux-aurora 7.1.12.aurora2-10, omarchy-mac-boot 20260926-1.45)"
    assert_equal "99ace40.361571887310001", m1["data-build"]
    assert_select "#benchmark-h264-decode-suite-1 tr.score-row .score-value", "691"
    assert_select "#benchmark-vulkan-suite-1 tr.score-row td.dim", "vkmark 2025.01"
    assert_select ".site-nav a.current", "Benchmarks"
  end

  test "scores from another suite are never compared with these" do
    upload_report golden(M2), machine: "a"
    upload_report run_with(2.0, suite: 2), machine: "b"

    get "/benchmarks"

    assert_equal 1, opengl_rows.size
    assert_select "#benchmark-opengl-suite-2 tr.score-row .score-value", "5,974"
    assert_select "#benchmark-opengl-suite-1 tr.score-row .score-value", "2,987"
  end

  test "runs without scores, and hidden reports, add nothing" do
    upload_report golden("m2-max-converged")                       # its benchmarks were skipped
    hidden = upload_report golden(M2), machine: "b"
    Report.find_by!(public_id: hidden["id"]).update!(hidden_at: Time.current)

    get "/benchmarks"

    assert_response :success
    assert_select ".score-row", 0
    assert_select ".card", /No report has a benchmark score yet/
  end

  test "a model page shows its scores against the best Mac, and the report shows each score" do
    upload_report golden(M2), machine: "a"
    m1 = upload_report run_with(0.6, on: "m1-pro-converged"), machine: "b"

    get "/models/j314s"
    assert_select "#benchmarks tr.score-row", 4
    assert_select %(#benchmarks tr.score-row[data-benchmark="opengl-suite-1"] .score-value), "1,792"
    assert_select %(#benchmarks tr.score-row[data-benchmark="opengl-suite-1"] .score-bar[style=?]), "--share: 60.0%"

    get path_of(m1["report_url"])
    assert_select "li#check-benchmark\\.opengl .score", /score 1,792 points \(glmark2 2023\.01, suite 1\)/
    assert_select "li#check-benchmark\\.hevc-decode .score a[href=?]", "/benchmarks#benchmark-hevc-decode-suite-1"
  end

  test "the comparison and every score download under CC0" do
    upload_report golden(M2), machine: "a"

    get "/api/v1/benchmarks.json"
    assert_response :success
    body = response.parsed_body
    assert_equal "CC0-1.0", body.dig("license", "id")
    opengl = body["benchmarks"].find { |benchmark| benchmark["check"] == "benchmark.opengl" }
    assert_equal({ "model" => "MacBook Pro (16-inch, M2 Max, 2023)", "board" => "j416c", "soc" => "t6021", "chip" => "M2 Max",
                   "stack" => "converged", "version" => "4.0.0", "build" => {
                     "id" => "1937418.362376005140001", "label" => "1937418.362376005140001", "words" => "1937418.362376005140001 (linux-aurora 7.1.12.aurora2-10, omarchy-mac-boot 20260926-1)",
                     "commit" => "1937418", "stamp" => "362376005140001", "linux_aurora" => "7.1.12.aurora2-10", "omarchy_mac_boot" => "20260926-1",
                     "candidate_set" => "apple-test-1937418f520b-20260926", "candidate_set_source" => "packages",
                     "image" => { "format" => "1", "platform" => "apple-silicon" }, "tool_version" => Report.first.tool_version
                   }, "score" => 2987, "low" => 2987, "high" => 2987, "machines" => 1,
                   "runs" => 1, "tools" => [ "glmark2 2023.01" ] }, opengl["rows"].sole)
    assert_not_includes response.body, Report.first.machine_id

    get "/api/v1/checks.csv"
    header, *rows = response.body.split("\r\n").map { |line| line.scan(/"((?:[^"]|"")*)"/).flatten }
    row = header.zip(rows.find { |r| r.include?("benchmark.vulkan") }).to_h
    assert_equal({ "score" => "6124", "score_unit" => "points", "score_tool" => "vkmark 2025.01", "score_suite" => "1" },
                 row.slice("score", "score_unit", "score_tool", "score_suite"))
  end

  test "a score outside the schema is refused" do
    [
      ->(score) { score["unit"] = "percent" },
      ->(score) { score["value"] = -1 },
      ->(score) { score["value"] = "2987" },
      ->(score) { score["tool"] = "glmark2 <script>" },
      ->(score) { score["serial"] = "C02XYZ" },
      ->(score) { score.delete("suite") }
    ].each do |change|
      report = golden(M2)
      change.call(report["checks"].first["score"])
      body = upload_report report

      assert_response :unprocessable_content
      assert_match "report schema v1", body["error"]
    end
    assert_equal 0, Report.count
  end
end
