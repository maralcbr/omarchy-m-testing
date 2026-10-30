require "test_helper"

# A model summary's per-check cell: which skipped checks still count.
class MatrixCellTest < ActiveSupport::TestCase
  FakeReport = Struct.new(:machine_key, :upload_order, :counted_checks, keyword_init: true) do
    def tester? = true
    def regressed_from(_feature) = nil
  end

  def check(id, status, outcome) = { "id" => id, "status" => status, "classification" => { "feature" => "thunderbolt", "outcome" => outcome } }

  def cell(*checks) = MatrixCell.from([ FakeReport.new(machine_key: "neo", upload_order: 1, counted_checks: checks) ], "thunderbolt", per_check: true)

  test "a skip on hardware the Mac doesn't have counts as not on this Mac" do
    assert_equal "not-applicable", cell(check("ports.thunderbolt", "skip", "not-applicable")).state
  end

  test "any other skip leaves the cell untested" do
    assert_equal "not-tested", cell(check("ports.thunderbolt", "skip", "not-tested")).state
  end
end
