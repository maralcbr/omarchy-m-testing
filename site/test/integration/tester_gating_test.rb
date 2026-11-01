require "test_helper"

# Seam B: tester runs colour the matrix on their own, and a candidate set's
# page shows the tester results per model and feature and whether the set is
# ready for promotion.
class TesterGatingTest < ActionDispatch::IntegrationTest
  M2 = "j416c".freeze
  CANDIDATE = "edge-2026.09.27".freeze

  setup do
    Tester.create!(login: "tester-one")
    Tester.create!(login: "tester-two")
  end

  def cell(board, feature)
    get "/matrix" unless response&.request&.path == "/matrix"
    css_select(%(tr.model-summary[data-board="#{board}"] td[data-feature="#{feature}"])).sole
  end

  def on_candidate(report, set = CANDIDATE) = report.tap { |r| r["system"]["candidate_set"] = set }

  GPU_FAILS = { "gpu.driver" => "fails", "gpu.vulkan" => "fails" }.freeze

  # The golden report with every failing check working (the goldens carry real regressions).
  def working(name, outcomes = {})
    fixed = golden(name)["checks"].select { |check| check.dig("classification", "outcome") == "fails" }.to_h { |check| [ check["id"], "works" ] }
    golden_with(name, fixed.merge(outcomes))
  end

  test "one tester run colours the cell on its own, framed as tester-verified" do
    bind_machine "a", "tester-one"
    upload_report golden("m2-max-image2"), machine: "a"

    get "/matrix"
    gpu = cell(M2, "gpu")
    assert_equal "works", gpu["data-state"]
    assert_equal "tester", gpu["data-verified"]
    assert_includes gpu["class"], "cell-works cell-tester"
    assert_match "works: tester-verified, 1 tester machine works", gpu["title"]
  end

  test "a machine bound to a handle off the allowlist is still one community machine" do
    bind_machine "a", "not-a-tester"
    upload_report golden("m2-max-image2"), machine: "a"

    get "/matrix"
    assert_equal "unconfirmed", cell(M2, "gpu")["data-state"]
  end

  test "tester runs outweigh community runs; a newer tester pass retires an older tester failure" do
    bind_machine "a", "tester-one"
    upload_report golden_with("m2-max-image2", GPU_FAILS), machine: "a"
    upload_report golden("m2-max-image2"), machine: "b"
    upload_report golden("m2-max-image2"), machine: "c"

    get "/matrix"
    assert_equal "fails", cell(M2, "gpu")["data-state"]
    assert_match "; 3 machines in all", cell(M2, "gpu")["title"]

    bind_machine "d", "tester-two"
    upload_report golden("m2-max-image2"), machine: "d"
    get "/matrix"
    assert_equal "works", cell(M2, "gpu")["data-state"]
  end

  test "a newer tester failure stands: an older pass doesn't retire it, so testers disagree" do
    bind_machine "a", "tester-one"
    bind_machine "d", "tester-two"
    upload_report golden("m2-max-image2"), machine: "a"
    upload_report golden_with("m2-max-image2", GPU_FAILS), machine: "d"

    get "/matrix"
    assert_equal "partial", cell(M2, "gpu")["data-state"]
  end

  test "a machine that fails after it passed can't retire another machine's failure" do
    bind_machine "a", "tester-one"
    bind_machine "d", "tester-two"
    upload_report golden("m2-max-image2"), machine: "a"
    upload_report golden_with("m2-max-image2", GPU_FAILS), machine: "d"
    upload_report golden_with("m2-max-image2", GPU_FAILS), machine: "a"

    get "/matrix"
    assert_equal "regression", cell(M2, "gpu")["data-state"]
  end

  test "a machine's latest run decides: a community run after its tester run takes the tester colour away" do
    bind_machine "a", "tester-one"
    upload_report golden("m2-max-image2"), machine: "a"
    TesterBinding.delete_all
    upload_report golden_with("m2-max-image2", GPU_FAILS), machine: "a"

    get "/matrix"
    gpu = cell(M2, "gpu")
    assert_equal "unconfirmed", gpu["data-state"]
    assert_nil gpu["data-verified"]
  end

  test "removing a tester from the allowlist takes their runs out of the matrix colours" do
    bind_machine "a", "tester-one"
    upload_report golden("m2-max-image2"), machine: "a"
    Tester.find_by!(login: "tester-one").destroy!

    get "/matrix"
    assert_equal "unconfirmed", cell(M2, "gpu")["data-state"]
  end

  test "matrix.json counts tester machines per cell" do
    bind_machine "a", "tester-one"
    upload_report golden("m2-max-image2"), machine: "a"

    get "/api/v1/matrix.json"
    m2 = response.parsed_body["rows"].find { |row| row["board"] == M2 }
    assert_equal({ "state" => "works", "tentative" => "works", "machines" => { "works" => 1 }, "tester_machines" => { "works" => 1 } }, m2.dig("cells", "gpu"))
  end

  test "a candidate set with only community runs waits for testers" do
    upload_report on_candidate(golden("m2-max-image2")), machine: "a"
    upload_report on_candidate(golden("m2-max-image2")), machine: "b"

    get "/candidates"
    assert_select %(tr#candidate-#{CANDIDATE.gsub(".", "\\.")}[data-verdict="waiting"])
    get "/candidates/#{CANDIDATE}"
    assert_response :success
    assert_select "#verdict", "Waiting for tester runs"
    assert_select "tr.matrix-row", 0
    assert_select "tr.report-row", 2
  end

  test "tester runs on every feature working make the set ready; the view is per model and feature, tester runs only" do
    bind_machine "a", "tester-one"
    upload_report on_candidate(working("m2-max-image2")), machine: "a"
    upload_report on_candidate(golden_with("m2-max-image2", GPU_FAILS)), machine: "b"
    upload_report golden_with("m2-max-image2", GPU_FAILS), machine: "a"

    get "/candidates/#{CANDIDATE}"
    assert_select %(#verdict[data-verdict="ready"]), "Ready for promotion: tester runs on 1 model, nothing failing"
    assert_select %(tr.matrix-row[data-board="#{M2}"] td[data-feature="gpu"][data-state="works"][data-verified="tester"])
    assert_select "tr.report-row", 2, "only the set's runs"
    assert_select "tr.report-row .badge-tester", 1
  end

  test "a tester's failure blocks the set and is listed" do
    bind_machine "a", "tester-one"
    bind_machine "b", "tester-two"
    upload_report on_candidate(working("m2-max-image2")), machine: "a"
    upload_report on_candidate(working("m1-pro-mx-mac", GPU_FAILS)), machine: "b"

    get "/candidates/#{CANDIDATE}"
    assert_select %(#verdict[data-verdict="blocked"]), "Not ready: tester runs found 1 failure"
    assert_select "#failures li.finding[data-state=fails]", 1
    assert_select "#failures li.finding", /MacBook Pro.*GPU/m
    get "/candidates"
    assert_select %(tr.candidate-row[data-verdict="blocked"])
  end

  test "hidden reports don't count, and an unknown set is not found" do
    bind_machine "a", "tester-one"
    upload_report on_candidate(golden("m2-max-image2")), machine: "a"
    Report.update_all(hidden_at: Time.current)

    get "/candidates/#{CANDIDATE}"
    assert_response :not_found
    get "/candidates"
    assert_select "tr.candidate-row", 0
  end

  test "the report page links its candidate set" do
    uploaded = upload_report on_candidate(golden("m2-max-image2"))
    get path_of(uploaded["report_url"])
    assert_select %(a[href="/candidates/#{CANDIDATE}"]), CANDIDATE
  end
end
