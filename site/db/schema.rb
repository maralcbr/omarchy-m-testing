# This file is auto-generated from the current state of the database. Instead
# of editing this file, please use the migrations feature of Active Record to
# incrementally modify your database, and then regenerate this schema definition.
#
# This file is the source Rails uses to define your schema when running `bin/rails
# db:schema:load`. When creating a new database, `bin/rails db:schema:load` tends to
# be faster and is potentially less error prone than running all of your
# migrations from scratch. Old migrations may fail to apply correctly if those
# migrations use external dependencies or application code.
#
# It's strongly recommended that you check this file into your version control system.

ActiveRecord::Schema[8.1].define(version: 2026_10_28_120000) do
  # These are extensions that must be enabled in order to support this database
  enable_extension "pg_catalog.plpgsql"

  create_table "reports", force: :cascade do |t|
    t.string "public_id", null: false
    t.string "deletion_token_digest", null: false
    t.integer "schema_version", null: false
    t.jsonb "body", null: false
    t.datetime "created_at", null: false
    t.datetime "updated_at", null: false
    t.datetime "hidden_at"
    t.string "machine_id"
    t.string "tester_login"
    t.index ["public_id"], name: "index_reports_on_public_id", unique: true
  end

  create_table "tester_bindings", force: :cascade do |t|
    t.string "machine_id", null: false
    t.string "github_login", null: false
    t.bigint "github_id", null: false
    t.datetime "created_at", null: false
    t.datetime "updated_at", null: false
    t.index ["github_login"], name: "index_tester_bindings_on_github_login"
    t.index ["machine_id"], name: "index_tester_bindings_on_machine_id", unique: true
  end

  create_table "testers", force: :cascade do |t|
    t.string "login", null: false
    t.bigint "github_id"
    t.datetime "created_at", null: false
    t.datetime "updated_at", null: false
    t.index ["login"], name: "index_testers_on_login", unique: true
  end
end
