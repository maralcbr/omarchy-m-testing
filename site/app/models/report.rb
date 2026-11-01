# One uploaded report. The body is the schema-validated report as the CLI sent
# it, less its machine signature; public_id is the unguessable id in its URL.
# Only a digest of the deletion token is stored; the token itself is returned
# once, at upload.
#
# machine_id groups reports by machine for the "two or more distinct machines
# agree" rule: "key:" and a keyed digest of the machine's verified public key
# (MachineSignature), never shown, never exported. Reports from before machine
# keys have "ip:" and a keyed digest of the uploader's network instead, and
# those without either (the very first ones) all count as a single machine.
#
# tester_login is the GitHub handle the machine was bound to when the report
# was uploaded (TesterBinding): the report is a tester run while that handle
# is on the allowlist (Tester). The handle is shown only to the admin.
class Report < ApplicationRecord
  has_secure_token :public_id, length: 24

  attr_reader :deletion_token

  validates :body, :schema_version, presence: true

  before_validation :issue_deletion_token, on: :create

  scope :visible, -> { where(hidden_at: nil) }
  scope :newest_first, -> { order(created_at: :desc, id: :desc) }

  # The packages whose version is "the Omarchy version" of a run, by preference.
  OMARCHY_PACKAGES = %w[omarchy omarchy-dev omarchy-mac].freeze

  STACK_WORDS = {
    "converged" => "converged",
    "mx-mac" => "mx-mac",
    "legacy-omarchy-mac" => "legacy omarchy-mac",
    "reference" => "reference"
  }.freeze

  def self.digest(token)
    OpenSSL::Digest::SHA256.hexdigest(token.to_s)
  end

  def deletion_token_matches?(token)
    token.present? && ActiveSupport::SecurityUtils.secure_compare(deletion_token_digest, self.class.digest(token))
  end

  def machine = body.fetch("machine")
  def system = body.fetch("system")
  def checks = body.fetch("checks")
  # Nobody was at the Mac (omarchy-m-test --unattended): only the automatic checks ran.
  def unattended? = body["unattended"] == true

  def model_name = machine.fetch("model")
  def short_model_name = model_name.delete_prefix("Apple ")
  def board = machine.fetch("board")
  def soc = machine.fetch("soc")
  def chip = machine.fetch("chip")

  # The SSD's capacity as Apple sells it; nil on reports from before the CLI recorded it.
  def storage = machine["storage"]
  def kernel = machine.fetch("kernel")
  def stack = system.fetch("stack")
  def stack_words = STACK_WORDS.fetch(stack, stack)
  def tool_version = body.dig("tool", "version")
  def candidate_set = system["candidate_set"]

  # The build the run is on (Build), or nil for a reference run.
  def build = defined?(@build) ? @build : (@build = Build.for(self))
  def build_words = build&.words

  # Failures the catalogue expects on this Mac: support not there yet, or no such hardware.
  EXPECTED_GAP_OUTCOMES = %w[not-in-aurora not-in-asahi not-in-omarchy not-applicable].freeze
  def self.expected_gap?(check) = check["status"] == "fail" && EXPECTED_GAP_OUTCOMES.include?(check.dig("classification", "outcome"))

  # pass, fail (not expected), gap (expected failures) and skip counts; confirmed: of the counted checks only.
  def result_counts(confirmed: false)
    @result_counts ||= {}
    @result_counts[confirmed] ||= (confirmed ? counted_checks : checks).each_with_object(Hash.new(0)) do |check, counts|
      counts[Report.expected_gap?(check) ? "gap" : check["status"]] += 1
    end
  end

  def package_version(name)
    system.fetch("packages").find { |package| package["name"] == name }&.fetch("version")
  end

  # The release a run is on, coarse enough that two machines on the same
  # release share it: the leading dotted numbers of the Omarchy package
  # ("4.0.0.alpha.quattro.r179...-1" is 4.0.0), or the distro for a reference run.
  def omarchy_version
    return system.fetch("distro") if stack == "reference"

    OMARCHY_PACKAGES.each do |name|
      version = package_version(name) or next
      return version[/\A\d+(\.\d+)*/] || version
    end
    "unknown"
  end

  # Model x stack/version x build: one row of the compatibility matrix.
  # by_build: false merges the builds of one release; by_model: true also merges releases, keeping stacks separate.
  def configuration(by_build: true, by_model: false)
    by_build = false if by_model
    Configuration.new(board:, stack:, version: (omarchy_version unless by_model), build: (build&.words if by_build), build_id: (build&.id if by_build))
  end

  # The machine key this report was signed with.
  def machine_key = machine_id || "unknown"

  # The machine each report counts as, for agreement between machines: reports
  # signed with one machine key, or uploaded by one signed-in handle on one board,
  # are one machine. Reinstalling a Mac gives it a new key, and its runs from
  # before mustn't outvote the ones after as a second Mac.
  def self.machine_keys(reports)
    parent = Hash.new { |hash, key| hash[key] = key }
    root = ->(key) { parent[key] == key ? key : parent[key] = root.(parent[key]) }
    reports.each { |report| parent[root.(report.machine_key)] = root.("#{report.tester_login}@#{report.board}") if report.tester_login }
    reports.to_h { |report| [ report, root.(report.machine_key) ] }
  end

  def self.machine_count(reports) = machine_keys(reports).values.uniq.size

  def hidden? = hidden_at.present?

  def tester? = Tester.allowlisted?(tester_login)

  # A human check answered yes by a bare Enter (the prompt's default), not a
  # typed y: shown as "works (unconfirmed)", and never counted towards a
  # feature's state, so it colours no matrix cell and gates nothing.
  def self.answered_by_default?(check) = check["answered_by_default"] == true

  # The checks that count towards feature states: all but those answered by default.
  def counted_checks = checks.reject { |check| Report.answered_by_default?(check) }

  # Result per catalogue feature this report tested, from its counted checks'
  # outcomes alone: feature id => ResultState, where a failure is "fails".
  def tested_states
    @tested_states ||= counted_checks.group_by { |check| check.dig("classification", "feature") }
                             .transform_values { |group| ResultState.combine(group.map { |check| check.dig("classification", "outcome") }) }
  end

  # tested_states, with a failure a "regression" where a verified earlier run
  # on the same model and stack passed (Regressions).
  def feature_states
    @feature_states ||= tested_states.to_h do |feature_id, state|
      [ feature_id, state == "fails" && regressed_from(feature_id) ? "regression" : state ]
    end
  end

  # The verified earlier pass a failing feature regressed from, or nil.
  def regressed_from(feature_id) = Regressions.current.pass_before(self, feature_id)

  def regression?(feature_id) = feature_states[feature_id] == "regression"

  # Upload order: the order runs happened in, as far as the site knows.
  def upload_order = [ created_at, id ]

  # The Aurora kernel package's version, or nil when the run isn't on Aurora.
  def aurora_version = package_version("linux-aurora")

  def to_param = public_id

  private

  def issue_deletion_token
    @deletion_token = SecureRandom.urlsafe_base64(32)
    self.deletion_token_digest = self.class.digest(@deletion_token)
  end
end
