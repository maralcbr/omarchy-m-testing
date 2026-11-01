# What macOS measures on a Mac (catalogue/macos-reference/<board>.json, reviewed
# files from scripts/macos-reference): shown beside Linux's benchmark scores as a
# description, never a verdict. Storage numbers are keyed by capacity, because an
# SSD's speed depends on its size, so they compare only at the same capacity.
class MacosReference
  # Benchmark check => the metric to show. No entry: macOS has no counterpart (glmark2 and vkmark don't run there).
  METRICS = { "benchmark.h264-decode" => "h264_decode_fps", "benchmark.hevc-decode" => "hevc_decode_fps" }.freeze
  STORAGE = "benchmark.storage".freeze
  STORAGE_METRIC = "read_mbps".freeze

  def self.dir
    Pathname(ENV.fetch("MACOS_REFERENCE_PATH") { Rails.root.join("..", "catalogue", "macos-reference").to_s })
  end

  def self.for(board)
    @references ||= {}
    return @references[board] if @references.key?(board)

    path = dir.join("#{board}.json")
    @references[board] = path.file? && board.match?(/\Aj[0-9]{3}[a-z]{0,2}\z/) ? new(JSON.parse(path.read)) : nil
  end

  def self.compared?(check_id) = METRICS.key?(check_id) || check_id == STORAGE

  def initialize(document)
    @document = document
  end

  # The macOS number for this benchmark, or nil. `capacity` matters for storage only.
  def value(check_id, capacity: nil)
    return @document.dig("storage", capacity, STORAGE_METRIC) if check_id == STORAGE

    @document.dig("metrics", METRICS[check_id]) if METRICS.key?(check_id)
  end

  def storage? = @document.fetch("storage", {}).any?

  def conditions
    found = @document.fetch("conditions")
    power = found["power"] == "ac" ? "on AC" : "on battery"
    "macOS #{@document.dig("macos", "version")}, #{power}, brightness #{found["brightness"]}, #{found["displays"]} #{"display".pluralize(found["displays"])}, #{@document["captured"]}"
  end
end
