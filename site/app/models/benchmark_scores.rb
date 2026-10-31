# Benchmark scores from the visible reports (checks[].score on passed
# benchmark.* checks), compared per Mac model, Omarchy stack/version and build.
#
# Scores are compared only within one benchmark: the same check id, suite
# (the CLI's fixed scenes, clip and settings) and unit. Each machine counts
# once per model and stack/version, with its latest score, so one Mac run ten
# times doesn't outweigh another; a row's score is the median of its machines'.
class BenchmarkScores
  NAMES = {
    "benchmark.opengl" => [ "OpenGL", "glmark2, off-screen at 1920x1080" ],
    "benchmark.vulkan" => [ "Vulkan", "vkmark, headless at 1920x1080" ],
    "benchmark.h264-decode" => [ "H.264 decode", "ffmpeg, VA-API hardware decode of a 1080p clip" ],
    "benchmark.hevc-decode" => [ "HEVC decode", "ffmpeg, VA-API hardware decode of a 1080p clip" ]
  }.freeze
  UNITS = { "points" => "points", "fps" => "frames/s" }.freeze

  # One measured run: a report's score for one benchmark.
  Score = Data.define(:report, :check_id, :value, :unit, :tool, :suite)

  Row = Data.define(:configuration, :model_name, :chip, :soc, :scores) do
    # Each machine's latest score, newest first.
    def latest_per_machine
      keys = Report.machine_keys(scores.map(&:report).uniq)
      scores.sort_by { |score| [ score.report.created_at, score.report.id ] }.reverse.uniq { |score| keys.fetch(score.report) }
    end
    def machines = latest_per_machine.size
    def runs = scores.size
    def values = latest_per_machine.map(&:value).sort
    def low = values.first
    def high = values.last
    def latest = latest_per_machine.first
    def tools = latest_per_machine.map(&:tool).uniq
    def tester? = latest_per_machine.any? { |score| score.report.tester? }

    def median
      middle = values.size / 2
      values.size.odd? ? values[middle] : (values[middle - 1] + values[middle]) / 2.0
    end
  end

  Benchmark = Data.define(:check_id, :suite, :unit, :rows) do
    def id = "#{check_id.delete_prefix("benchmark.")}-suite-#{suite}"
    def name = NAMES.dig(check_id, 0) || check_id
    def how = NAMES.dig(check_id, 1)
    def unit_words = UNITS.fetch(unit, unit)
    def best = rows.map(&:median).max
    def rows_for_board(board) = rows.select { |row| row.configuration.board == board }
  end

  def self.visible = new(Report.visible.to_a)

  def initialize(reports)
    @reports = reports
  end

  # Every benchmark with scores: in catalogue order, newest suite first; rows best first.
  def benchmarks
    @benchmarks ||= scores.group_by { |score| [ score.check_id, score.suite, score.unit ] }.map do |(check_id, suite, unit), group|
      rows = group.group_by { |score| score.report.configuration }.map do |configuration, row_scores|
        latest = row_scores.max_by { |score| [ score.report.created_at, score.report.id ] }.report
        Row.new(configuration:, model_name: latest.short_model_name, chip: latest.chip, soc: latest.soc, scores: row_scores)
      end
      Benchmark.new(check_id:, suite:, unit:, rows: rows.sort_by { |row| [ -row.median, row.model_name ] })
    end.sort_by { |benchmark| [ Catalogue.check_ids.index(benchmark.check_id) || Catalogue.check_ids.size, -benchmark.suite ] }
  end

  def empty? = benchmarks.empty?

  # The benchmarks this Mac model has scores in.
  def for_board(board) = benchmarks.select { |benchmark| benchmark.rows_for_board(board).any? }

  def as_json(*)
    {
      comparison: "scores compare only within one benchmark (check, suite and unit); each machine counts once per model and stack/version, with its latest score; score is the median over machines",
      benchmarks: benchmarks.map do |benchmark|
        {
          check: benchmark.check_id, name: benchmark.name, suite: benchmark.suite, unit: benchmark.unit,
          rows: benchmark.rows.map do |row|
            {
              model: row.model_name, board: row.configuration.board, soc: row.soc, chip: row.chip,
              stack: row.configuration.stack, version: row.configuration.version, build: row.latest.report.build&.as_json,
              score: row.median, low: row.low, high: row.high, machines: row.machines, runs: row.runs, tools: row.tools
            }
          end
        }
      end
    }
  end

  private

  def scores
    @scores ||= @reports.flat_map do |report|
      report.checks.filter_map do |check|
        score = check["score"]
        next unless check["status"] == "pass" && score.is_a?(Hash) && check["id"].to_s.start_with?("benchmark.")

        Score.new(report:, check_id: check["id"], value: score["value"], unit: score["unit"], tool: score["tool"], suite: score["suite"])
      end
    end
  end
end
