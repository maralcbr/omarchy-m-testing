require "test_helper"

# Seam B: a report from an unattended run (omarchy-m-test --unattended) is accepted and says so.
class UnattendedRunTest < ActionDispatch::IntegrationTest
  test "an unattended report is accepted and its page says nobody was at the Mac" do
    body = upload_report golden("m2-max-image2").merge("unattended" => true)
    assert_response :created

    get path_of(body.fetch("report_url"))
    assert_match "nobody was at the Mac", css_select("#unattended").sole.text
  end

  test "an attended report doesn't say unattended" do
    body = upload_report golden("m2-max-image2")
    get path_of(body.fetch("report_url"))
    assert_select "#unattended", 0
  end

  test "unattended can only be true" do
    upload_report golden("m2-max-image2").merge("unattended" => false)
    assert_response :unprocessable_content
  end
end
