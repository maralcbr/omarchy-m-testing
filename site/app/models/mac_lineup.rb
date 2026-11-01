# The bring-up table on /models: every Mac in the catalogue's boards, with or
# without reports, so the Macs that still need testing or bringing online show.
# Each row is a group of boards bought as one Mac (the M3 Air's J613 and J615)
# with its GPU and built-in display state, taken from the visible reports on
# those boards (MatrixCell over all of them: each machine's latest actual
# outcomes, tester runs first, community runs once machines agree). An
# untested Mac shows what the catalogue expects from Asahi and Aurora instead.
# Boards with reports that the file doesn't list are added from the reports.
class MacLineup
  AREAS = { "gpu" => "gpu", "display" => "dcp" }.freeze # area => catalogue feature
  WORDS = { "missing" => "not supported yet" }.freeze # bring-up words where ResultState's don't read well

  # A row's state for one area, in the site's colour code (ResultState): from
  # the runs (cell) when any board was tested, else from the catalogue (note).
  Status = Data.define(:state, :source, :cell, :note) do
    def tested? = source == "runs"
    def unconfirmed? = tested? && cell.unconfirmed?
    def words = MacLineup.words(state).then { |words| tested? ? words : "catalogue: #{words}" }
  end

  def self.words(state) = WORDS.fetch(state) { ResultState.words(state) }

  # One board's reports (oldest first) and what they found per area.
  Board = Data.define(:board, :reports, :results) do
    def tested? = reports.any?
    def tester_runs = reports.count(&:tester?)
    def last_tested_at = reports.last&.created_at
    def result(area) = results.fetch(area)
  end

  Row = Data.define(:name, :chip, :boards, :statuses) do
    def status(area) = statuses.fetch(area)
    def tested? = boards.any?(&:tested?)
    def untested_boards = boards.reject(&:tested?)
  end

  # The catalogue's boards grouped by lineup, in the catalogue's order.
  def self.models
    @models ||= Catalogue.boards.group_by { |_board, info| info.fetch("lineup") }.map do |name, boards|
      { "name" => name, "chip" => boards.first.last.fetch("chip"), "boards" => boards.map(&:first) }
    end
  end

  def self.visible = new(Report.visible.to_a)

  def initialize(reports)
    @reports = reports.sort_by(&:upload_order).group_by(&:board)
  end

  def rows
    @rows ||= begin
      listed = self.class.models.map { |model| row(model.fetch("name"), model.fetch("chip"), model.fetch("boards")) }
      known = self.class.models.flat_map { |model| model.fetch("boards") }
      unlisted = (@reports.keys - known).map do |board|
        latest = @reports.fetch(board).last
        row(latest.short_model_name, Catalogue.chip_for_soc(latest.soc), [ board ])
      end
      listed + unlisted.sort_by { |r| [ Catalogue.chips.keys.index(r.chip) || Catalogue.chips.size, r.name ] }
    end
  end

  def boards = rows.flat_map(&:boards)
  def untested_boards = boards.reject(&:tested?)

  private

  def row(name, chip, boards)
    boards = boards.map do |board|
      reports = @reports.fetch(board, [])
      Board.new(board:, reports:, results: AREAS.transform_values { |feature| MatrixCell.from(reports, feature, per_check: true) })
    end
    reports = boards.flat_map(&:reports).sort_by(&:upload_order)
    statuses = AREAS.to_h { |area, feature| [ area, found(reports, feature) || expected(feature, chip, boards.first&.board) ] }
    Row.new(name:, chip:, boards:, statuses:)
  end

  # What the runs on the row's boards found, or nil when none tested the feature.
  def found(reports, feature_id)
    cell = MatrixCell.from(reports, feature_id, per_check: true)
    Status.new(state: cell.display_state, source: "runs", cell:, note: nil) if cell.tallies.any?
  end

  # The catalogue's view: Aurora supports it, or matches Asahi where Asahi has it.
  def expected(feature_id, chip, board)
    states = Catalogue.expected(Catalogue.feature(feature_id), chip, board)
    return Status.new(state: "unknown", source: "catalogue", cell: nil, note: "not in the catalogue") if states.nil?

    asahi = states["asahi"] || {}
    aurora = states["aurora"] || {}
    state =
      if asahi["status"] == "absent" then "not-applicable"
      elsif aurora["status"] == "supported" || (aurora["status"] == "asahi" && Catalogue::ASAHI_SUPPORTED.include?(asahi["status"])) then "works"
      else "missing"
      end
    note = aurora["status"] == "supported" ? "Aurora #{aurora["version"]}".strip : "Asahi: #{asahi["cell"] || asahi["status"]}"
    Status.new(state:, source: "catalogue", cell: nil, note: (note unless state == "not-applicable"))
  end
end
