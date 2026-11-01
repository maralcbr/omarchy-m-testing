# The feature catalogue shared with the CLI (../catalogue/catalogue.json from
# the repo root, or CATALOGUE_PATH when the site is deployed without the rest
# of the repo). The site knows a check id only if the catalogue does, and
# words each classification outcome the way the CLI does.
module Catalogue
  LAYERS = %w[asahi aurora omarchy].freeze
  ASAHI_SUPPORTED = %w[upstream linux-asahi yes].freeze
  LAYER_WORDS = { "asahi" => "Asahi hardware", "aurora" => "Aurora addition", "omarchy" => "Omarchy integration" }.freeze

  def self.path
    Pathname(ENV.fetch("CATALOGUE_PATH") { Rails.root.join("..", "catalogue", "catalogue.json").to_s })
  end

  def self.data
    @data ||= JSON.parse(path.read)
  end

  def self.version = data.fetch("catalogue_version")
  def self.check_ids = data.fetch("checks").keys
  def self.outcome_words(outcome) = data.fetch("outcomes").fetch(outcome, outcome)
  def self.asahi_credit = data.dig("sources", "asahi", "credit")
  def self.features = data.fetch("features")
  def self.chips = data.fetch("chips")
  # Every Apple Silicon Mac board: its name, chip, SoC, lineup and hardware facts.
  def self.boards = data.fetch("boards")

  def self.feature(id)
    @features ||= features.index_by { |f| f.fetch("id") }
    @features[id]
  end

  def self.feature_name(id) = feature(id)&.fetch("name") || id

  # Check ids per feature id, in catalogue order.
  def self.checks_for(feature_id)
    @checks_by_feature ||= data.fetch("checks").group_by { |_check, feature| feature }.transform_values { |pairs| pairs.map(&:first) }
    @checks_by_feature.fetch(feature_id, [])
  end

  # The features some check tests, Asahi layer first, then Aurora, then Omarchy.
  def self.tested_features
    @tested_features ||= features.select { |f| checks_for(f.fetch("id")).any? }.sort_by.with_index { |f, i| [ LAYERS.index(f.fetch("layer")), i ] }
  end

  # The catalogue's chip generation key ("m2-pro-max-ultra") for a SoC id ("t6021").
  def self.chip_for_soc(soc)
    chips.find { |_key, info| info.fetch("socs").include?(soc) }&.first
  end

  def self.chip_name(key) = chips.dig(key, "name") || key

  # Each layer's expected state ({"status" => ..., "version"/"cell" => ...}) for
  # this chip generation and board, as the CLI's classifier resolves it; nil
  # when the catalogue doesn't cover the chip.
  def self.expected(feature, chip, board = nil)
    return nil if chip.nil? || !feature.fetch("chips").key?(chip)

    states = feature.fetch("chips").fetch(chip).dup
    states.merge!(feature.dig("models", board) || {}) if board
    if !states.key?("asahi") && (linked = feature["asahi_feature"] && feature(feature["asahi_feature"]))
      linked_states = expected(linked, chip, board)
      states["asahi"] = linked_states["asahi"] if linked_states&.key?("asahi")
    end
    states
  end

  # Check ids in the report that this catalogue doesn't know.
  def self.unknown_check_ids(report)
    Array(report["checks"]).filter_map { |check| check["id"] }.uniq - check_ids
  end
end
