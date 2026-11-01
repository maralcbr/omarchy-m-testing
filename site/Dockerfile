# The production image for Railway. Build from the repository root, since the
# site reads the shared report schema, the feature catalogue and the installer
# from next to site/:  docker build -f site/Dockerfile .
ARG RUBY_VERSION=3.4.7
FROM docker.io/library/ruby:$RUBY_VERSION-slim AS base

WORKDIR /app/site
RUN apt-get update -qq && \
    apt-get install --no-install-recommends -y curl libpq5 libyaml-0-2 && \
    rm -rf /var/lib/apt/lists /var/cache/apt/archives
ENV RAILS_ENV="production" \
    BUNDLE_DEPLOYMENT="1" \
    BUNDLE_PATH="/usr/local/bundle" \
    BUNDLE_WITHOUT="development:test"

FROM base AS build
RUN apt-get update -qq && \
    apt-get install --no-install-recommends -y build-essential git libpq-dev libyaml-dev pkg-config && \
    rm -rf /var/lib/apt/lists /var/cache/apt/archives
COPY site/Gemfile site/Gemfile.lock ./
RUN bundle install && rm -rf ~/.bundle "$BUNDLE_PATH"/ruby/*/cache "$BUNDLE_PATH"/ruby/*/bundler/gems/*/.git
COPY site/ ./
RUN SECRET_KEY_BASE_DUMMY=1 bin/rails assets:precompile

FROM base
COPY --from=build "$BUNDLE_PATH" "$BUNDLE_PATH"
COPY --from=build /app/site /app/site
COPY schema/ /app/schema/
COPY catalogue/catalogue.json /app/catalogue/catalogue.json
COPY catalogue/macos-reference/ /app/catalogue/macos-reference/
COPY installer/install.sh /app/installer/install.sh
RUN groupadd --system --gid 1000 rails && \
    useradd rails --uid 1000 --gid 1000 --create-home --shell /bin/bash && \
    chown -R rails:rails log tmp
USER 1000:1000
EXPOSE 3000
CMD ["bin/rails", "server", "-b", "0.0.0.0"]
