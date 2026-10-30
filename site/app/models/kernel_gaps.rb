# What to work on next in the kernel: features Asahi supports that Aurora
# lacks or hasn't verified (from the catalogue), and what the visible reports
# found missing or unclaimed on real Macs.
class KernelGaps
  # A catalogue gap: an Asahi-supported feature and the chips where Aurora lacks it.
  CatalogueGap = Data.define(:feature, :chips)
  # A reported gap: one outcome of one feature on one chip, with the reports that found it.
  ReportedGap = Data.define(:outcome, :feature_id, :soc, :chip, :reports) do
    def machines = Report.machine_count(reports)
    def models = reports.map(&:short_model_name).uniq
  end

  # Hardware no driver claimed, from the reports' inventory: one device-tree
  # node type (its compatible string) with what the catalogue says it is.
  # outcome is unknown-hardware when the catalogue doesn't know it.
  UnclaimedHardware = Data.define(:compatible, :outcome, :feature_id, :reports) do
    def unknown? = outcome == "unknown-hardware"
    def machines = Report.machine_count(reports)
    def models = reports.map(&:short_model_name).uniq
    def chips = reports.map { |report| [ report.chip, report.soc ] }.uniq
  end

  REPORTED_OUTCOMES = %w[not-in-aurora not-in-asahi unknown-hardware].freeze

  def initialize(reports)
    @reports = reports
  end

  # Aurora status unsupported where Asahi supports it.
  def missing_in_aurora = catalogue_gaps(%w[unsupported])

  # Aurora status unknown where Asahi supports it.
  def unverified_in_aurora = catalogue_gaps(%w[unknown])

  def reported(outcome)
    found = Hash.new { |hash, key| hash[key] = [] }
    @reports.each do |report|
      report.checks.each do |check|
        next unless check.dig("classification", "outcome") == outcome

        key = [ check.dig("classification", "feature"), report.soc, report.chip ]
        found[key] << report unless found[key].include?(report)
      end
    end
    found.map { |(feature_id, soc, chip), reports| ReportedGap.new(outcome:, feature_id:, soc:, chip:, reports:) }
         .sort_by { |gap| [ Catalogue.feature_name(gap.feature_id), gap.soc ] }
  end

  # Unclaimed hardware across the visible reports, unknown hardware first.
  def unclaimed_hardware
    found = Hash.new { |hash, key| hash[key] = [] }
    @reports.each do |report|
      inventory = report.body["inventory"]
      next unless inventory.is_a?(Hash)

      Array(inventory["unclaimed"]).each do |entry|
        next unless entry.is_a?(Hash) && entry["compatible"].is_a?(String)

        key = [ entry["compatible"], entry["outcome"], entry["feature"] ]
        found[key] << report unless found[key].include?(report)
      end
    end
    found.map { |(compatible, outcome, feature_id), reports| UnclaimedHardware.new(compatible:, outcome:, feature_id:, reports:) }
         .sort_by { |hardware| [ hardware.unknown? ? 0 : 1, hardware.compatible ] }
  end

  private

  def catalogue_gaps(aurora_statuses)
    Catalogue.features.filter_map do |feature|
      chips = Catalogue.chips.keys.select do |chip|
        states = Catalogue.expected(feature, chip) or next false
        Catalogue::ASAHI_SUPPORTED.include?(states.dig("asahi", "status")) && aurora_statuses.include?(states.dig("aurora", "status"))
      end
      CatalogueGap.new(feature:, chips:) if chips.any?
    end
  end
end
