require "test_helper"

# Model summaries span releases and builds, with their evidence and history intact.
class ModelResultsTest < ActionDispatch::IntegrationTest
  OLD = "7.1.12-2-2-ARCH"
  NEW = "7.1.12-2-11-ARCH"

  setup do
    Tester.create!(login: "tester")
    bind_machine("a", "tester")
  end

  def upload(outcome, kernel: OLD, release: "4.0.0-1", machine: "a", stack: "converged")
    report = golden_with("m2-max-image2", "display.backlight" => outcome)
    report["machine"]["kernel"] = kernel
    report["system"]["stack"] = stack
    report["system"]["packages"].find { |package| package["name"] == "omarchy" }["version"] = release
    report["system"]["packages"].find { |package| package["name"] == "linux-aurora" }["version"] = kernel
    response = upload_report(report, machine:)
    assert_response :created
    Report.find_by!(public_id: response["id"])
  end

  def summary_cell(feature = "brightness", stack: "converged")
    css_select(%(tr.model-summary[data-board="j416c"][data-stack="#{stack}"] td[data-feature="#{feature}"])).sole
  end

  test "the default model view replaces a failure with a later pass and preserves it through a skip" do
    upload "fails"
    passed = upload "works", kernel: NEW, release: "4.1.0-1"
    upload "not-tested", kernel: "7.1.13-1-1-ARCH", release: "4.2.0-1"

    get "/matrix"
    assert_response :success
    assert_select ".matrix-view-switch a[aria-current=page][data-view=model]", "Model"
    assert_select "tr.model-summary", 1
    assert_equal "works", summary_cell["data-state"]
    assert_equal [ NEW ], JSON.parse(summary_cell["data-kernels"])
    assert_select summary_cell, "a[href=?]", report_path(passed)
    assert_select "tbody.matrix-history tr.matrix-row", 3
    assert_select "tbody.matrix-history[hidden]", 0, "builds remain available without JavaScript"
    assert_select "button.model-toggle[hidden][aria-controls]", 1
    assert_select "form.filter-bar input[name=group][value=model]"
    assert_select ".filter-exports a[href=?]", "/api/v1/matrix.json?group=model"
    assert_select ".filter-count[data-count=3]"

    get "/"
    assert_response :success
    assert_select "tr.model-summary", 1
    assert_equal "works", summary_cell["data-state"]
    assert_select "tbody.matrix-history tr.matrix-row", 3
  end

  test "a later failure on another release supersedes the pass and a skip keeps the regression" do
    upload "works"
    failure = upload "fails", kernel: NEW, release: "4.1.0-1"
    upload "not-tested", kernel: "7.1.13-1-1-ARCH", release: "4.2.0-1"

    get "/matrix", params: { group: "model", state: "regression" }
    assert_equal "regression", summary_cell["data-state"]
    assert_select summary_cell, "a[href=?]", report_path(failure)
    assert_select "tbody.matrix-history tr.matrix-row", 3, "the earlier pass stays available in the history"
  end

  test "upload order decides the latest result even when a machine retests an older kernel" do
    upload "works", kernel: NEW
    upload "fails", kernel: OLD, release: "4.1.0-1"

    get "/matrix", params: { group: "model" }
    assert_equal "regression", summary_cell["data-state"]
    assert_equal [ OLD ], JSON.parse(summary_cell["data-kernels"])

    get "/matrix", params: { group: "model", kernel: NEW }
    assert_equal "works", summary_cell["data-state"]
    assert_equal [ NEW ], JSON.parse(summary_cell["data-kernels"])
    assert_select "tbody.matrix-history tr.matrix-row", 1
    assert_select ".filter-chip[data-filter=kernel][href=?]", "/matrix?group=model"
    assert_select ".filter-exports a[href=?]", "/api/v1/matrix.json?group=model&kernel=#{NEW}"
  end

  test "a carried result identifies its actual source kernel and date" do
    earlier = nil
    travel_to(Time.utc(2026, 10, 4, 12)) { earlier = upload "works" }
    travel_to(Time.utc(2026, 10, 4, 13)) { upload "not-tested", kernel: NEW }

    get "/matrix", params: { group: "model" }
    assert_equal [ OLD ], JSON.parse(summary_cell["data-kernels"])
    assert_equal earlier.created_at.utc.iso8601, summary_cell["data-tested-at"]
    assert_match OLD, summary_cell["title"]
    assert_match "2026-10-04 12:00 UTC", summary_cell["title"]
    assert_select summary_cell, "a[href=?]", report_path(earlier)
    assert_select summary_cell, "a[aria-label=?]", summary_cell["title"]
  end

  test "skipping one check retains its previous result when another check of the feature passes" do
    failed = golden_with("m2-max-image2", "gpu.driver" => "fails", "gpu.vulkan" => "works")
    upload_report failed
    assert_response :created
    later = golden_with("m2-max-image2", "gpu.driver" => "not-tested", "gpu.vulkan" => "works")
    later["machine"]["kernel"] = NEW
    upload_report later
    assert_response :created

    get "/matrix", params: { group: "model" }
    assert_equal "partial", summary_cell("gpu")["data-state"]
    assert_equal [ OLD, NEW ].sort, JSON.parse(summary_cell("gpu")["data-kernels"]).sort
  end

  test "confirmed summaries still show the unconfirmed runs that established agreement" do
    upload "works", machine: "b"
    upload "works", machine: "c", kernel: NEW

    get "/matrix", params: { group: "model", confirmed: "1" }
    assert_equal "works", summary_cell["data-state"]
    assert_select "tbody.matrix-history tr.matrix-row", 2
    assert_select "tbody.matrix-history td[data-feature=brightness][data-state=unconfirmed]", 2
  end

  test "a newer community result cannot claim the source of a tester-confirmed result" do
    upload "works"
    upload "fails", kernel: NEW, machine: "b"

    get "/matrix", params: { group: "model" }
    assert_equal "works", summary_cell["data-state"]
    assert_equal "tester", summary_cell["data-verified"]
    assert_equal [ OLD ], JSON.parse(summary_cell["data-kernels"])

    get "/matrix", params: { group: "model", runs: "community" }
    assert_equal "unconfirmed", summary_cell["data-state"]
    assert_equal [ NEW ], JSON.parse(summary_cell["data-kernels"])
  end

  test "different machines still disagree and different stacks have separate summaries" do
    Tester.create!(login: "tester-two")
    bind_machine("b", "tester-two")
    upload "works"
    upload "fails", kernel: NEW, machine: "b"
    upload "works", kernel: NEW, stack: "mx-mac"

    get "/matrix", params: { group: "model" }
    assert_select "tr.model-summary", 2
    assert_equal "partial", summary_cell["data-state"]
    assert_equal "works", summary_cell(stack: "mx-mac")["data-state"]
    assert_equal [ NEW, OLD ].sort, JSON.parse(summary_cell["data-kernels"]).sort
  end

  test "the model export contains the same results, filters and source metadata" do
    upload "works"
    latest = upload "fails", kernel: NEW, release: "4.1.0-1"
    upload "not-tested", kernel: NEW, release: "4.2.0-1"

    get "/api/v1/matrix.json", params: { group: "model", runs: "tester" }
    assert_response :success
    body = response.parsed_body
    assert_equal "model", body["group"]
    assert_equal 3, body["runs"]
    assert_equal({ "runs" => "tester" }, body["filters"])
    row = body["rows"].sole
    assert_nil row["version"]
    assert_equal %w[4.2.0 4.1.0 4.0.0], row["releases"]
    assert_equal "regression", row["cells"]["brightness"]["state"]
    assert_equal [ NEW ], row["cells"]["brightness"]["kernels"]
    assert_equal latest.created_at.utc.iso8601, row["cells"]["brightness"]["last_tested_at"]
  end
end
