# One feature on one configuration (model x stack/version), aggregated over
# the visible reports. Each machine counts once, with the latest state it
# tested. Tester runs (Report#tester?) colour the cell on their own: the state
# the tester machines agree on, partial when they disagree. Without tester
# runs, the cell only takes a colour when two or more distinct machines agree;
# until then it shows the community reports, labelled as unconfirmed.
# Machines agreeing on different states make it partial.
class MatrixCell
  AGREEMENT = 2

  attr_reader :tallies, :tester_tallies, :reports

  # reports: oldest first, all on the same configuration. only_testers: count tester runs only.
  def self.from(reports, feature_id, only_testers: false, per_check: false)
    return from_checks(reports, feature_id, only_testers:) if per_check

    latest = {} # machine => [state, tester run?], from its latest report that tested the feature
    tested = []
    reports.each do |report|
      tester = report.tester?
      next if only_testers && !tester

      state = report.feature_states[feature_id]
      next if state.nil? || state == "not-tested"

      tested << report
      latest[report.machine_key] = [ state, tester ]
    end
    new(latest.values.map(&:first).tally, tested.reverse, latest.values.select(&:last).map(&:first).tally)
  end

  # Across builds, a partial retest must retain the outcomes of individual skipped checks; a skip on hardware the
  # Mac doesn't have counts, as not-applicable.
  def self.from_checks(reports, feature_id, only_testers:)
    latest = Hash.new { |hash, machine| hash[machine] = {} }
    tested = []
    reports.each do |report|
      next if only_testers && !report.tester?

      checks = report.counted_checks.select do |check|
        outcome = check.dig("classification", "outcome")
        check.dig("classification", "feature") == feature_id && outcome != "not-tested" &&
          (check["status"] != "skip" || outcome == "not-applicable")
      end
      next if checks.empty?

      tested << report
      checks.each { |check| latest[report.machine_key][check["id"]] = [ check.dig("classification", "outcome"), report ] }
    end
    votes = latest.values.map do |checks|
      sources = checks.values.map(&:last).uniq.sort_by(&:upload_order).reverse
      state = ResultState.combine(checks.values.map(&:first))
      state = "regression" if state == "fails" && sources.first.regressed_from(feature_id)
      [ state, sources.all?(&:tester?), sources ]
    end
    tester_votes = votes.select { |_, tester, _| tester }
    contributing = tester_votes.any? ? tester_votes : votes
    sources = contributing.flat_map(&:last).uniq.sort_by(&:upload_order).reverse
    new(votes.map(&:first).tally, tested.reverse, tester_votes.map(&:first).tally, sources:)
  end
  private_class_method :from_checks

  def initialize(tallies, reports, tester_tallies = {}, hidden: false, sources: nil)
    @tallies = tallies
    @reports = reports
    @tester_tallies = tester_tallies
    @hidden = hidden
    @sources = sources
  end

  # Hidden by the confirmed-only filter (ReportFilter): what community machines don't agree on yet.
  def hidden? = @hidden

  # This cell with confirmed results only: an unconfirmed one is hidden.
  def confirmed_only = unconfirmed? ? MatrixCell.new({}, [], hidden: true) : self

  def machines = tallies.values.sum
  def tester_machines = tester_tallies.values.sum
  def tester? = tester_tallies.any?

  # The contributing reports, including checks retained from earlier runs in a model summary.
  # When tester runs decide the state, community reports cannot claim its provenance.
  def sources
    return @sources if @sources

    latest = reports.uniq(&:machine_key)
    tester? ? latest.select(&:tester?) : latest
  end

  # The colour state, or nil while the community reports don't agree yet.
  def state
    return if hidden?
    return "not-tested" if tallies.empty?
    return (tester_tallies.one? ? tester_tallies.keys.first : "partial") if tester?

    agreed = tallies.select { |_state, count| count >= AGREEMENT }.keys
    return agreed.first if agreed.one?

    "partial" if agreed.many?
  end

  def confirmed? = state.present? && tallies.any?
  def unconfirmed? = state.nil?

  # What the community reports say before machines agree; partial on a tie.
  def tentative
    return "not-tested" if tallies.empty?

    leaders = tallies.select { |_state, count| count == tallies.values.max }.keys
    leaders.one? ? leaders.first : "partial"
  end

  def display_state = state || tentative

  def css_class
    if hidden? then "cell cell-not-tested cell-hidden"
    elsif unconfirmed? then "cell cell-unconfirmed cell-hint-#{tentative}"
    elsif tester? then "cell cell-#{state} cell-tester"
    else "cell cell-#{state}"
    end
  end

  def summary
    if hidden?
      "unconfirmed community results, hidden (confirmed only)"
    elsif tallies.empty?
      "not tested"
    elsif tester?
      "#{ResultState.words(state)}: tester-verified, #{describe(tester_tallies, "tester machine")}" +
        (machines > tester_machines ? "; #{machines} machines in all" : "")
    elsif confirmed?
      "#{ResultState.words(state)}: #{describe(tallies, "machine")}"
    else
      "community, unconfirmed: #{describe(tallies, "machine")}; colours once #{AGREEMENT} machines agree or a tester confirms"
    end
  end

  # Each state's machine count, without the verdict ("2 machines works, 1 machine fails").
  def counts = describe(tester? ? tester_tallies : tallies, tester? ? "tester machine" : "machine")

  private

  def describe(tallies, noun)
    tallies.sort_by { |state, _| ResultState::ORDER.index(state) }
           .map { |state, count| "#{count} #{count == 1 ? noun : noun.pluralize} #{ResultState.words(state)}" }
           .join(", ")
  end
end
