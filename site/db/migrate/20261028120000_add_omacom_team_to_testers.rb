# Adds the omacom GitHub org members (bar omarchybot) to the tester allowlist.
# Handles already on it are skipped; each GitHub account is pinned at its next
# sign-in, as with every handle.
class AddOmacomTeamToTesters < ActiveRecord::Migration[8.1]
  LOGINS = %w[
    acelogic acrogenesis afoliveira bastidotnet birkskyum bjarneo chriskearney
    csfh dhh djdeveloperr emirb erikmelton erykwieliczko getaaron hancore-linux
    jdvmi00 kevinmcconnell kwilczynski liamray11 malik-na maralcbr mdisec
    oldjobobo outfoxxed ryanrhughes ryanthetide scottjones spencerbull tahayvr
    themartiano tobi yamz8
  ].freeze

  def up
    now = connection.quote(Time.current.utc)
    rows = LOGINS.map { |login| "(#{connection.quote(login)}, #{now}, #{now})" }.join(", ")
    execute "INSERT INTO testers (login, created_at, updated_at) VALUES #{rows} ON CONFLICT (login) DO NOTHING"
  end

  # Leaves the handles: the admin may have added some by hand. Remove them at /admin/testers.
  def down; end
end
