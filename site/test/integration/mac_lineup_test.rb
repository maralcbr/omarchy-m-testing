require "test_helper"

# The bring-up table on /models: every Mac, tested or not, with its GPU and display state from the runs.
class MacLineupTest < ActionDispatch::IntegrationTest
  def lineup_row(board) = css_select(%(tr.lineup-row[data-boards~="#{board}"])).sole
  def status(board, area) = lineup_row(board).css(%(td.lineup-status[data-area="#{area}"])).sole
  def state_and_source(board, area) = status(board, area).then { |td| [ td["data-state"], td["data-source"] ] }

  test "untested Macs show and ask for testing" do
    get "/models"
    assert_response :success
    assert_select "tr.lineup-row", MacLineup.models.size
    assert_equal "false", lineup_row("j504")["data-tested"]
    assert_match "needs testing", lineup_row("j504").css(%(td.lineup-testing div[data-board="j504"])).text
  end

  test "an untested Mac shows what the catalogue expects" do
    get "/models"
    assert_equal [ "works", "catalogue" ], state_and_source("j416c", "gpu")
    assert_equal [ "works", "catalogue" ], state_and_source("j413", "display")
    assert_equal [ "missing", "catalogue" ], state_and_source("j700", "gpu")
    assert_equal [ "missing", "catalogue" ], state_and_source("j514c", "gpu")
    assert_match "catalogue: not supported yet", status("j514c", "gpu").text
  end

  test "the runs decide once a Mac is tested, over the catalogue" do
    upload_report golden_with("m2-max-image2", "gpu.vulkan" => "fails"), machine: "a"
    Report.update_all(tester_login: "tester-one")
    Tester.create!(login: "tester-one")

    get "/models"
    assert_equal [ "partial", "runs" ], state_and_source("j416c", "gpu")
    assert_equal [ "works", "runs" ], state_and_source("j416c", "display")
    assert_no_match "unconfirmed", status("j416c", "gpu").text
  end

  test "a reinstalled tester Mac's newer run replaces its older one" do
    Tester.create!(login: "tester-one")
    upload_report golden_with("m2-max-image2", "gpu.driver" => "not-in-asahi", "gpu.vulkan" => "not-in-asahi"), machine: "a"
    upload_report golden("m2-max-image2"), machine: "b", ip: "10.0.0.2"
    Report.update_all(tester_login: "tester-one")

    get "/models"
    assert_equal [ "works", "runs" ], state_and_source("j416c", "gpu")
    assert_match "1 tester machine works", status("j416c", "gpu")["title"]
  end

  test "one community machine is unconfirmed; two that agree confirm it" do
    upload_report golden("m2-max-image2"), machine: "a"
    get "/models"
    assert_equal [ "works", "runs" ], state_and_source("j416c", "gpu")
    assert_match "unconfirmed", status("j416c", "gpu").text

    upload_report golden("m2-max-image2"), machine: "b", ip: "10.0.0.2"
    get "/models"
    assert_no_match "unconfirmed", status("j416c", "gpu").text
  end

  test "a tested board shows its runs and what they found" do
    upload_report golden("m2-max-image2")

    get "/models"
    row = lineup_row("j416c")
    assert_equal "true", row["data-tested"]
    assert_match "1 run", row.css(%(td.lineup-testing div[data-board="j416c"])).text
    assert_equal "works", row.css(%(td[data-area="gpu"] .lineup-tested[data-board="j416c"])).sole["data-state"]
    assert_match "needs testing", row.css(%(td.lineup-testing div[data-board="j414c"])).text
  end
end
