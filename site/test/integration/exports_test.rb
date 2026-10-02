require "test_helper"

# Seam B: every piece of public data downloads as JSON or CSV, under CC0, and
# never includes hidden or deleted reports or the machine grouping key.
class ExportsTest < ActionDispatch::IntegrationTest
  def parse_csv(text)
    text.split("\r\n").map { |line| line.scan(/"((?:[^"]|"")*)"/).flatten.map { |field| field.gsub('""', '"') } }
  end

  setup do
    @m2 = upload_report golden("m2-max-image2"), machine: "a"
    @m1 = upload_report golden("m1-pro-mx-mac"), machine: "b"
  end

  test "reports.json has every report exactly as uploaded, under CC0" do
    get "/api/v1/reports.json"

    assert_response :success
    body = response.parsed_body
    assert_equal "CC0-1.0", body.dig("license", "id")
    assert_match "CC BY 3.0", body["note"]
    assert_equal [ @m2["id"], @m1["id"] ], body["reports"].map { |r| r["id"] }
    assert_equal golden("m2-max-image2"), body["reports"][0]["report"]
    assert_equal @m2["report_url"], body["reports"][0]["url"]
    assert_not_includes response.body, Report.first.machine_id
    assert_not_includes response.body, TestMachines.public_key("a").split.last
    assert_not_includes response.body, "SSH SIGNATURE"
    assert_not_includes response.body, "deletion"
  end

  test "reports.csv has one row per report" do
    get "/api/v1/reports.csv"

    assert_response :success
    assert_equal "text/csv", response.media_type
    rows = parse_csv(response.body)
    assert_equal DataExport::REPORT_COLUMNS, rows[0]
    assert_equal 3, rows.size
    m2 = DataExport::REPORT_COLUMNS.zip(rows[1]).to_h
    assert_equal({ "id" => @m2["id"], "board" => "j416c", "stack" => "converged", "omarchy_version" => "4.0.0", "checks" => "72",
                   "pass" => "37", "fail" => "4", "skip" => "31", "encryption" => "on" },
                 m2.slice("id", "board", "stack", "omarchy_version", "checks", "pass", "fail", "skip", "encryption"))
  end

  test "uploaded text that looks like a spreadsheet formula is exported as text" do
    upload_report golden("m2-max-image2").tap { |r| r["machine"]["kernel"] = "=HYPERLINK(\"http://example.com\")" }
    get "/api/v1/reports.csv"

    assert_includes response.body, %("'=HYPERLINK(""http://example.com"")")
  end

  test "checks.csv has one row per check result" do
    get "/api/v1/checks.csv"

    assert_response :success
    rows = parse_csv(response.body)
    assert_equal DataExport::CHECK_COLUMNS, rows[0]
    assert_equal 1 + golden("m2-max-image2")["checks"].size + golden("m1-pro-mx-mac")["checks"].size, rows.size
    failed = rows.map { |row| DataExport::CHECK_COLUMNS.zip(row).to_h }.find { |row| row["report_id"] == @m2["id"] && row["status"] == "fail" }
    assert_equal({ "check_id" => "setup.first-boot-hardware", "outcome" => "fails", "feature" => "first-boot-hardware-setup",
                   "layer" => "omarchy", "expected_aurora" => "supported", "expected_omarchy" => "supported", "expected_asahi" => "" },
                 failed.slice("check_id", "outcome", "feature", "layer", "expected_asahi", "expected_aurora", "expected_omarchy"))
  end

  test "matrix.json has each cell's state and per-machine agreement" do
    upload_report golden("m2-max-image2"), machine: "c"
    get "/api/v1/matrix.json"

    assert_response :success
    body = response.parsed_body
    assert_equal "CC0-1.0", body.dig("license", "id")
    m2 = body["rows"].find { |row| row["board"] == "j416c" }
    assert_equal({ "state" => "works", "tentative" => "works", "machines" => { "works" => 2 }, "tester_machines" => {} }, m2.dig("cells", "gpu"))
    m1 = body["rows"].find { |row| row["board"] == "j314s" }
    assert_equal({ "state" => nil, "tentative" => "fails", "machines" => { "fails" => 1 }, "tester_machines" => {} }, m1.dig("cells", "vendor-firmware"))
  end

  test "the data page links every export" do
    get "/data"

    assert_response :success
    %w[/api/v1/reports.json /api/v1/reports.csv /api/v1/checks.csv /api/v1/matrix.json].each do |path|
      assert_select "a[href=?]", path
    end
    assert_select "a[href=?]", "https://creativecommons.org/publicdomain/zero/1.0/"
  end

  test "hidden and deleted reports are left out of every export" do
    Report.find_by!(public_id: @m1["id"]).update!(hidden_at: Time.current)
    token = Rack::Utils.parse_query(URI(@m2["deletion_url"]).query)["token"]
    delete path_of(@m2["report_url"]), params: { token: }

    get "/api/v1/reports.json"
    assert_empty response.parsed_body["reports"]
    get "/api/v1/reports.csv"
    assert_equal 1, parse_csv(response.body).size
    get "/api/v1/checks.csv"
    assert_equal 1, parse_csv(response.body).size
    get "/api/v1/matrix.json"
    assert_empty response.parsed_body["rows"]
  end
end
