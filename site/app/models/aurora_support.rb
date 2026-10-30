# The Aurora feature-support table: what the Aurora kernel (omacom/linux)
# supports per chip generation, from tester-verified runs on it, that is
# visible tester runs (Report#tester?) with the linux-aurora package. A cell
# is the state the tester machines on that chip generation agree on, each
# machine counted once with its latest run that tested the feature
# (MatrixCell), partial when they disagree. It sits next to the catalogue's
# expected Aurora and Asahi states, and its sources are the runs behind it
# and the Aurora versions they ran. Kernel features only: the Asahi and
# Aurora layers, not Omarchy's integration.
class AuroraSupport
  LAYERS = %w[asahi aurora].freeze
  PACKAGE = "linux-aurora".freeze

  Cell = Data.define(:feature, :chip, :result, :expected) do
    def state = result.state || "not-tested"
    def tested? = result.tallies.any?
    def reports = result.reports # newest first
    def machines = result.tester_machines
    def aurora_versions = reports.map(&:aurora_version).uniq
    def expected_aurora = expected&.dig("aurora")
    def expected_asahi = expected&.dig("asahi")
    def summary = tested? ? result.summary : "no tester run on Aurora tested this"
  end

  Chip = Data.define(:key, :reports, :cells) do
    def name = Catalogue.chip_name(key)
    def cell(feature_id) = cells.fetch(feature_id)
    def machines = Report.machine_count(reports)
    def models = reports.map(&:short_model_name).uniq
    def aurora_versions = reports.map(&:aurora_version).uniq
    def socs = reports.map(&:soc).uniq
  end

  def self.visible = new(Report.visible.where.not(tester_login: nil).to_a)

  def initialize(reports)
    @reports = reports.select { |report| report.tester? && report.aurora_version }.sort_by(&:upload_order)
  end

  def features = Catalogue.tested_features.select { |feature| LAYERS.include?(feature.fetch("layer")) }
  def empty? = chips.empty?

  # Chip generations with tester runs on Aurora, in catalogue order.
  def chips
    @chips ||= @reports.group_by { |report| Catalogue.chip_for_soc(report.soc) }.except(nil)
                       .sort_by { |key, _| Catalogue.chips.keys.index(key) }
                       .map do |key, reports|
      cells = features.to_h do |feature|
        [ feature.fetch("id"), Cell.new(feature:, chip: key, result: MatrixCell.from(reports, feature.fetch("id"), only_testers: true),
                                        expected: Catalogue.expected(feature, key)) ]
      end
      Chip.new(key:, reports: reports.reverse, cells:)
    end
  end

  # Catalogue chip generations no tester has run Aurora on yet.
  def untested_chips = Catalogue.chips.keys - chips.map(&:key)

  def as_json(url_for)
    {
      catalogue_version: Catalogue.version,
      method: "per chip generation, from visible tester runs with the #{PACKAGE} package: each tester machine counts once, with its latest run that tested the feature; " \
              "state is what the tester machines agree on, partial when they disagree, not-tested without one",
      sources: {
        runs: "tester-verified reports on this site, linked per cell",
        aurora: Catalogue.data.dig("sources", "aurora"),
        asahi: Catalogue.data.dig("sources", "asahi").slice("title", "url", "license", "credit")
      },
      features: features.map { |feature| { id: feature["id"], name: feature["name"], layer: feature["layer"] } },
      chips: chips.map do |chip|
        {
          chip: chip.key, name: chip.name, socs: chip.socs, models: chip.models, machines: chip.machines, runs: chip.reports.size,
          aurora_versions: chip.aurora_versions,
          cells: chip.cells.transform_values do |cell|
            {
              state: cell.state, tester_machines: cell.result.tester_tallies,
              expected: { aurora: cell.expected_aurora, asahi: cell.expected_asahi },
              aurora_versions: cell.aurora_versions, reports: cell.reports.map { |report| url_for.call(report) }
            }
          end
        }
      end,
      untested_chips: untested_chips
    }
  end
end
