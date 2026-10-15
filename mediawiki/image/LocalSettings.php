<?php
# ═══════════════════════════════════════════════════════════════════════════
# papaia-addon-mediawiki — LocalSettings.php
# by Fidonis GmbH · https://fidonis.de
#
# Every deployment-specific value comes from the container environment (the
# add-on .env). Values are read with getenv() at request time instead of being
# substituted into this file: a secret containing a quote or a backslash can
# then never break the PHP syntax, and changing a value needs a container
# restart, not an image rebuild.
#
# Site-specific additions belong in /etc/papaia-mediawiki/settings.d/*.php
# (bind-mounted from MW_SETTINGS_DIR), loaded at the end of this file.
# ═══════════════════════════════════════════════════════════════════════════

if ( !defined( 'MEDIAWIKI' ) ) {
	exit;
}

use MediaWiki\MediaWikiServices;

function papaiaEnv( string $name, ?string $default = null ): ?string {
	$value = getenv( $name );
	return ( $value === false || trim( $value ) === '' ) ? $default : trim( $value );
}

function papaiaEnvBool( string $name, bool $default ): bool {
	$value = papaiaEnv( $name );
	if ( $value === null ) {
		return $default;
	}
	return in_array( strtolower( $value ), [ '1', 'true', 'yes', 'on' ], true );
}

/** Split a comma-separated list, dropping empty entries. */
function papaiaList( ?string $raw ): array {
	return array_values( array_filter(
		array_map( 'trim', explode( ',', (string)$raw ) ),
		'strlen'
	) );
}

function papaiaRequire( string $name ): string {
	$value = papaiaEnv( $name );
	if ( $value === null ) {
		throw new RuntimeException( "papaia-mediawiki: $name is not set" );
	}
	return $value;
}

## ── Site ──────────────────────────────────────────────────────────────────
$wgSitename = papaiaEnv( 'MW_SITE_NAME', 'Wiki' );
# Fixed, so renaming the site does not rename the project namespace.
$wgMetaNamespace = 'Project';

# Browser-facing URL; OIDC redirect URIs and every generated link use it.
$wgServer = rtrim( papaiaRequire( 'MW_SERVER' ), '/' );
$wgCanonicalServer = $wgServer;
$wgScriptPath = '';
$wgResourceBasePath = $wgScriptPath;

$wgLanguageCode = papaiaEnv( 'MW_LANG', 'en' );
$wgLocaltimezone = papaiaEnv( 'MW_TIMEZONE', 'UTC' );

$wgSecretKey = papaiaRequire( 'MW_SECRET_KEY' );
$wgUpgradeKey = papaiaRequire( 'MW_UPGRADE_KEY' );

$wgLogos = [ 'icon' => "$wgResourceBasePath/resources/assets/change-your-logo.svg" ];
$wgRightsPage = '';
$wgRightsText = '';

## ── Database ──────────────────────────────────────────────────────────────
$wgDBtype = 'mysql';
$wgDBserver = papaiaEnv( 'MW_DB_HOST', 'mediawiki-db' );
$wgDBname = papaiaEnv( 'MW_DB_NAME', 'mediawiki' );
$wgDBuser = papaiaEnv( 'MW_DB_USER', 'mediawiki' );
$wgDBpassword = papaiaRequire( 'MW_DB_PASSWORD' );
$wgDBprefix = '';
$wgDBTableOptions = 'ENGINE=InnoDB, DEFAULT CHARSET=binary';

## ── Caching ───────────────────────────────────────────────────────────────
# APCu ships with the image. Sessions live in the database so they survive a
# container restart.
$wgMainCacheType = CACHE_ACCEL;
$wgSessionCacheType = CACHE_DB;
$wgParserCacheType = CACHE_DB;
$wgMessageCacheType = CACHE_ACCEL;

## ── Mail ──────────────────────────────────────────────────────────────────
# Off unless MW_SMTP_HOST (host:port) is set. The relay is used without
# authentication, as a local relay accepting mail from this host expects.
$papaiaSmtp = papaiaEnv( 'MW_SMTP_HOST' );
$wgEnableEmail = $papaiaSmtp !== null;
$wgEnableUserEmail = false;
$wgEmailAuthentication = false;
$wgPasswordSender = papaiaEnv( 'MW_EMAIL_FROM', 'wiki@localhost' );
$wgEmergencyContact = $wgPasswordSender;
$wgEnotifUserTalk = $wgEnableEmail;
$wgEnotifWatchlist = $wgEnableEmail;
if ( $papaiaSmtp !== null ) {
	[ $papaiaSmtpHost, $papaiaSmtpPort ] = array_pad( explode( ':', $papaiaSmtp, 2 ), 2, '25' );
	$wgSMTP = [
		'host' => $papaiaSmtpHost,
		'IDHost' => parse_url( $wgServer, PHP_URL_HOST ),
		'localhost' => parse_url( $wgServer, PHP_URL_HOST ),
		'port' => (int)$papaiaSmtpPort,
		'auth' => false,
	];
}

## ── Uploads ───────────────────────────────────────────────────────────────
$wgEnableUploads = papaiaEnvBool( 'MW_ENABLE_UPLOADS', true );
$wgUseImageMagick = true;
$wgImageMagickConvertCommand = '/usr/bin/convert';
# Conservative: images and PDF only. SVG is left out on purpose -- an SVG
# can carry script, and MediaWiki's filter is the only thing standing
# between an upload and a stored XSS.
$wgFileExtensions = papaiaList( papaiaEnv( 'MW_FILE_EXTENSIONS', 'png,gif,jpg,jpeg,webp,pdf' ) );
$wgMaxUploadSize = (int)papaiaEnv( 'MW_MAX_UPLOAD_MB', '50' ) * 1024 * 1024;
# Files are served through img_auth.php, which checks read permission; the
# image's Apache config denies direct access to /images.
$wgUploadPath = "$wgScriptPath/img_auth.php";

## ── Skin and editing ──────────────────────────────────────────────────────
wfLoadSkin( 'Vector' );
$wgDefaultSkin = 'vector-2022';

wfLoadExtension( 'ParserFunctions' );
wfLoadExtension( 'Cite' );
wfLoadExtension( 'WikiEditor' );
wfLoadExtension( 'VisualEditor' );
$wgDefaultUserOptions['visualeditor-enable'] = 1;
$wgDefaultUserOptions['visualeditor-editor'] = 'visualeditor';

## ── Access: private wiki ──────────────────────────────────────────────────
$wgGroupPermissions['*']['read'] = false;
$wgGroupPermissions['*']['edit'] = false;
$wgGroupPermissions['*']['createaccount'] = false;
# PluggableAuth creates the wiki account on the first Keycloak login.
$wgGroupPermissions['*']['autocreateaccount'] = true;
$wgGroupPermissions['user']['read'] = true;
$wgGroupPermissions['user']['edit'] = true;
# Pages reachable before login, so the OIDC round-trip can start and finish.
$wgWhitelistRead = [ 'Special:UserLogin', 'Special:PluggableAuthLogin', 'Special:UserLogout' ];

# API clients sign in per user through OAuth (see below); shared bot
# passwords would bypass that.
$wgEnableBotPasswords = false;

## ── Authentication: Keycloak (PluggableAuth + OpenIDConnect) ──────────────
wfLoadExtension( 'PluggableAuth' );
wfLoadExtension( 'OpenIDConnect' );

# MW_ENABLE_LOCAL_LOGIN=true is the break-glass switch (see README): it shows
# the password form again and turns off the automatic redirect to Keycloak.
$papaiaLocalLogin = papaiaEnvBool( 'MW_ENABLE_LOCAL_LOGIN', false );
$wgPluggableAuth_EnableLocalLogin = $papaiaLocalLogin;
$wgPluggableAuth_EnableAutoLogin = !$papaiaLocalLogin && papaiaEnvBool( 'MW_AUTO_LOGIN', true );
$wgPluggableAuth_EnableLocalProperties = false;

# Username from preferred_username; real name and e-mail from the token.
$wgOpenIDConnect_UseRealNameAsUserName = false;
$wgOpenIDConnect_UseEmailNameAsUserName = false;
# Off by default: linking an existing account by e-mail lets anyone who can
# set that address in the identity provider take the account over.
$wgOpenIDConnect_MigrateUsersByEmail = papaiaEnvBool( 'MW_OIDC_MIGRATE_BY_EMAIL', false );

## ── Access tiers ──────────────────────────────────────────────────────────
# MW_NAMESPACES          Name:id[,Name:id...]   id even, >= 3000
# MW_NAMESPACE_READ_GROUPS Name=group[,group...][;Name=...]
# MW_SYSOP_GROUPS        groups whose members become sysop + bureaucrat
#
# Group names are the values of the groups claim; each one becomes a wiki
# group of the same name, synced on every login. The local admin is a member
# of PAPAIA_ALL_TIERS_GROUP and passes every tier.
const PAPAIA_ALL_TIERS_GROUP = 'all-tiers';

/** @var array<int,array{name:string,groups:string[]}> $papaiaTiers keyed by namespace id */
$papaiaTiers = [];
$papaiaReadGroups = [];
foreach ( explode( ';', (string)papaiaEnv( 'MW_NAMESPACE_READ_GROUPS', '' ) ) as $papaiaRule ) {
	if ( trim( $papaiaRule ) === '' ) {
		continue;
	}
	if ( !preg_match( '/^\s*([A-Za-z][A-Za-z0-9_]*)\s*=\s*(.+)$/', $papaiaRule, $m ) ) {
		throw new RuntimeException( "papaia-mediawiki: MW_NAMESPACE_READ_GROUPS: cannot parse '$papaiaRule'" );
	}
	$papaiaReadGroups[$m[1]] = papaiaList( $m[2] );
}
foreach ( papaiaList( papaiaEnv( 'MW_NAMESPACES', '' ) ) as $papaiaNsSpec ) {
	if ( !preg_match( '/^([A-Za-z][A-Za-z0-9_]*):(\d+)$/', $papaiaNsSpec, $m ) ) {
		throw new RuntimeException( "papaia-mediawiki: MW_NAMESPACES: cannot parse '$papaiaNsSpec' (expected Name:id)" );
	}
	[ , $papaiaNsName, $papaiaNsId ] = $m;
	$papaiaNsId = (int)$papaiaNsId;
	if ( $papaiaNsId < 3000 || $papaiaNsId % 2 !== 0 || isset( $papaiaTiers[$papaiaNsId] ) ) {
		throw new RuntimeException( "papaia-mediawiki: MW_NAMESPACES: id $papaiaNsId for $papaiaNsName must be even, >= 3000 and unique" );
	}
	if ( empty( $papaiaReadGroups[$papaiaNsName] ) ) {
		throw new RuntimeException( "papaia-mediawiki: MW_NAMESPACE_READ_GROUPS has no groups for namespace $papaiaNsName" );
	}
	$papaiaTiers[$papaiaNsId] = [ 'name' => $papaiaNsName, 'groups' => $papaiaReadGroups[$papaiaNsName] ];
	unset( $papaiaReadGroups[$papaiaNsName] );
}
if ( $papaiaReadGroups ) {
	throw new RuntimeException( 'papaia-mediawiki: MW_NAMESPACE_READ_GROUPS names namespaces missing from MW_NAMESPACES: '
		. implode( ', ', array_keys( $papaiaReadGroups ) ) );
}

wfLoadExtension( 'Lockdown' );

foreach ( $papaiaTiers as $papaiaNsId => $papaiaTier ) {
	$papaiaAllowed = array_values( array_unique( array_merge( $papaiaTier['groups'], [ PAPAIA_ALL_TIERS_GROUP ] ) ) );
	$papaiaEditRight = 'papaia-edit-' . strtolower( $papaiaTier['name'] );
	foreach ( [ $papaiaNsId, $papaiaNsId + 1 ] as $papaiaId ) {
		# Lockdown: every action -- read, edit, move, history, ... -- only for
		# the listed groups. Also removes the namespace from search for
		# everyone else.
		$wgNamespacePermissionLockdown[$papaiaId]['*'] = $papaiaAllowed;
		# Second line of defence for writes, independent of Lockdown.
		$wgNamespaceProtection[$papaiaId] = [ $papaiaEditRight ];
		$wgNamespacesWithSubpages[$papaiaId] = true;
		# Not searched by default: members tick the namespace under
		# "Advanced search" once; MediaWiki remembers the choice.
		$wgNamespacesToBeSearchedDefault[$papaiaId] = false;
	}
	$wgExtraNamespaces[$papaiaNsId] = $papaiaTier['name'];
	$wgExtraNamespaces[$papaiaNsId + 1] = $papaiaTier['name'] . '_talk';
	$wgContentNamespaces[] = $papaiaNsId;
	$wgVisualEditorAvailableNamespaces[$papaiaNsId] = true;
	foreach ( $papaiaAllowed as $papaiaGroup ) {
		$wgGroupPermissions[$papaiaGroup][$papaiaEditRight] = true;
	}
	# OAuth sessions only keep rights some grant covers; without this a
	# member signed in through the MCP server could read a tier but not edit
	# it. The right itself stays limited to the groups.
	$wgGrantPermissions['editpage'][$papaiaEditRight] = true;
}

# Special:Export bypasses the per-page read check on some paths; restrict it
# to the groups that can read everything anyway.
$wgSpecialPageLockdown['Export'] = [ 'sysop', PAPAIA_ALL_TIERS_GROUP ];

/**
 * Restricted namespace ids (subject and talk) the given user may not read.
 * @return int[]
 */
function papaiaHiddenNamespaces( $user ): array {
	global $papaiaTiers;
	static $cache = [];
	if ( !$papaiaTiers ) {
		return [];
	}
	$key = $user->getName();
	if ( !isset( $cache[$key] ) ) {
		$groups = MediaWikiServices::getInstance()->getUserGroupManager()->getUserEffectiveGroups( $user );
		$hidden = [];
		foreach ( $papaiaTiers as $nsId => $tier ) {
			if ( !array_intersect( $groups, array_merge( $tier['groups'], [ PAPAIA_ALL_TIERS_GROUP ] ) ) ) {
				$hidden[] = $nsId;
				$hidden[] = $nsId + 1;
			}
		}
		$cache[$key] = $hidden;
	}
	return $cache[$key];
}

/** The tier (subject namespace id) a namespace belongs to, or null. */
function papaiaTierOf( ?int $ns ): ?int {
	global $papaiaTiers;
	if ( $ns === null ) {
		return null;
	}
	$subject = $ns - ( $ns % 2 );
	return isset( $papaiaTiers[$subject] ) ? $subject : null;
}

# Transclusion ({{:Internal:Page}}, msgnw:, raw:, expandtemplates, parse
# with text) bypasses Lockdown: the parser fetches the source without a
# permission check, and the rendered result lands in the shared parser
# cache, where every later reader gets it. A page may therefore transclude
# from a tier only if it lives in that same tier. The rule depends on the
# two titles alone, never on the viewer, so cached output stays correct.
#
# The context title alone is not enough: action=parse, expandtemplates,
# Special:ExpandTemplates and the VisualEditor/REST transforms accept any
# title as context, so a reader outside the tier could pass a tier title and
# transclude from it. Such a request is also refused for a logged-in viewer
# who cannot read the tier. Anonymous and maintenance contexts (job runner,
# CLI) keep the title rule only: they cannot read the wiki at all, and a
# render from there may land in the parser cache, where a viewer-based skip
# would leave tier members with an incomplete page.
$wgHooks['BeforeParserFetchTemplateRevisionRecord'][] = static function (
	$contextTitle, $title, &$skip, &$revRecord
) {
	$sourceTier = papaiaTierOf( $title->getNamespace() );
	if ( $sourceTier === null ) {
		return;
	}
	if ( papaiaTierOf( $contextTitle ? $contextTitle->getNamespace() : null ) !== $sourceTier ) {
		$skip = true;
		return;
	}
	$viewer = RequestContext::getMain()->getUser();
	if ( $viewer->isRegistered() && in_array( $sourceTier, papaiaHiddenNamespaces( $viewer ), true ) ) {
		$skip = true;
	}
};

function papaiaNsCondition( string $column, array $hidden ): string {
	return $column . ' NOT IN (' . implode( ',', array_map( 'intval', $hidden ) ) . ')';
}

# Lockdown guards the pages themselves. Listings that show edit summaries or
# content from many pages at once are filtered here, per viewer.

# Special:RecentChanges, Special:Watchlist, Special:RecentChangesLinked and
# their Atom/RSS feeds.
$wgHooks['ChangesListSpecialPageQuery'][] = static function (
	$name, &$tables, &$fields, &$conds, &$query_options, &$join_conds, $opts
) {
	$hidden = papaiaHiddenNamespaces( RequestContext::getMain()->getUser() );
	if ( $hidden ) {
		$conds[] = papaiaNsCondition( 'rc_namespace', $hidden );
	}
};

# Special:NewPages.
$wgHooks['SpecialNewpagesConditions'][] = static function (
	$special, $opts, &$conds, &$tables, &$fields, &$join_conds
) {
	$hidden = papaiaHiddenNamespaces( RequestContext::getMain()->getUser() );
	if ( $hidden ) {
		$conds[] = papaiaNsCondition( 'rc_namespace', $hidden );
	}
};

# Special:Contributions.
$wgHooks['ContribsPager::getQueryInfo'][] = static function ( $pager, &$queryInfo ) {
	$hidden = papaiaHiddenNamespaces( $pager->getUser() );
	if ( $hidden ) {
		$queryInfo['conds'][] = papaiaNsCondition( 'page_namespace', $hidden );
	}
};

# Action API query modules (allpages, recentchanges, logevents,
# usercontribs, allrevisions, search, watchlist, ...): drop result entries
# from hidden namespaces after the module ran. Filtering the result rather
# than the SQL covers every module that reports an "ns" field, including
# those that never offer a query hook -- at the cost of short pages, which
# continuation handles.
$wgHooks['APIQueryAfterExecute'][] = static function ( $module ) {
	$hidden = papaiaHiddenNamespaces( $module->getUser() );
	if ( !$hidden ) {
		return;
	}
	$path = [ 'query', $module->getModuleName() ];
	$result = $module->getResult();
	$data = $result->getResultData( $path );
	if ( !is_array( $data ) ) {
		return;
	}
	$changed = false;
	foreach ( $data as $key => $item ) {
		if ( is_int( $key ) && is_array( $item ) && isset( $item['ns'] )
			&& in_array( (int)$item['ns'], $hidden, true )
		) {
			unset( $data[$key] );
			$changed = true;
		}
	}
	if ( $changed ) {
		$result->addValue( [ 'query' ], $module->getModuleName(), $data,
			ApiResult::OVERRIDE | ApiResult::NO_SIZE_CHECK );
	}
};

# Action API prop modules (prop=revisions, info, ...) attach their data to the
# page entries under query.pages. MediaWiki withholds the content of a page
# the caller cannot read, but not revision metadata such as edit summaries.
# Strip a hidden page down to the fields that only name it.
$wgHooks['APIAfterExecute'][] = static function ( $module ) {
	if ( $module->getModuleName() !== 'query' ) {
		return;
	}
	$hidden = papaiaHiddenNamespaces( $module->getUser() );
	if ( !$hidden ) {
		return;
	}
	$result = $module->getResult();
	$pages = $result->getResultData( [ 'query', 'pages' ] );
	if ( !is_array( $pages ) ) {
		return;
	}
	$changed = false;
	foreach ( $pages as $key => $page ) {
		if ( !is_array( $page ) || !isset( $page['ns'] ) || !in_array( (int)$page['ns'], $hidden, true ) ) {
			continue;
		}
		$kept = array_intersect_key( $page, array_flip( [ 'pageid', 'ns', 'title', 'missing', 'invalid' ] ) );
		if ( $kept !== $page ) {
			$pages[$key] = $kept;
			$changed = true;
		}
	}
	if ( $changed ) {
		$result->addValue( [ 'query' ], 'pages', $pages, ApiResult::OVERRIDE | ApiResult::NO_SIZE_CHECK );
	}
};

# Action API export (action=query&export): refuse outright. Full-text search:
# drop hidden namespaces from the requested ones, so a client that searches
# every content namespace (as MCP clients do) still gets the visible
# matches; refuse only when nothing visible is left.
$wgHooks['ApiCheckCanExecute'][] = static function ( $module, $user, &$message ) {
	$hidden = papaiaHiddenNamespaces( $user );
	if ( !$hidden || $module->getModuleName() !== 'query' ) {
		return true;
	}
	$request = $module->getMain()->getRequest();
	if ( $request->getCheck( 'export' ) || $request->getCheck( 'exportnowrap' ) ) {
		$message = ApiMessage::create( 'apierror-permissiondenied-generic', 'permissiondenied' );
		return false;
	}
	foreach ( [ 'srnamespace', 'gsrnamespace' ] as $param ) {
		if ( !$request->getCheck( $param ) ) {
			continue;
		}
		$requested = array_map( 'intval', explode( '|', (string)$request->getVal( $param, '' ) ) );
		$visible = array_values( array_diff( $requested, $hidden ) );
		if ( !$visible ) {
			$message = ApiMessage::create( 'apierror-permissiondenied-generic', 'permissiondenied' );
			return false;
		}
		$request->setVal( $param, implode( '|', $visible ) );
	}
	return true;
};

## ── Group sync from the groups claim ──────────────────────────────────────
# Mapped, not "sync all": only groups that mean something here are touched,
# so Keycloak's housekeeping roles (offline_access, default-roles-*) do not
# show up as wiki groups. Mapped groups are also removed again on login when
# the claim no longer carries them.
# The rule key is the claim name, which OpenIDConnect exposes as an attribute.
$papaiaGroupsClaim = papaiaEnv( 'OIDC_GROUPS_CLAIM', 'groups' );
$papaiaGroupMap = [];
foreach ( $papaiaTiers as $papaiaTier ) {
	foreach ( $papaiaTier['groups'] as $papaiaGroup ) {
		$papaiaGroupMap[$papaiaGroup] = [ $papaiaGroupsClaim => [ $papaiaGroup ] ];
	}
}
$papaiaSysopGroups = papaiaList( papaiaEnv( 'MW_SYSOP_GROUPS', '' ) );
if ( $papaiaSysopGroups ) {
	$papaiaGroupMap['sysop'] = [ $papaiaGroupsClaim => $papaiaSysopGroups ];
	$papaiaGroupMap['bureaucrat'] = [ $papaiaGroupsClaim => $papaiaSysopGroups ];
	$papaiaGroupMap['interface-admin'] = [ $papaiaGroupsClaim => $papaiaSysopGroups ];
}
foreach ( array_keys( $papaiaGroupMap ) as $papaiaGroup ) {
	# Declares the group, so it is listed under Special:ListGroupRights.
	$wgGroupPermissions[$papaiaGroup]['read'] = true;
}

$papaiaOidc = [
	'providerURL' => papaiaRequire( 'OIDC_ISSUER' ),
	'clientID' => papaiaEnv( 'MW_OIDC_CLIENT_ID', 'mediawiki' ),
	'clientsecret' => papaiaRequire( 'KC_MEDIAWIKI_CLIENT_SECRET' ),
	'scope' => [ 'openid', 'profile', 'email' ],
];
$wgPluggableAuth_Config[papaiaEnv( 'MW_LOGIN_BUTTON_LABEL', 'Log in with Keycloak' )] = [
	'plugin' => 'OpenIDConnect',
	'data' => $papaiaOidc,
	'groupsyncs' => $papaiaGroupMap ? [ [
		'type' => 'mapped',
		'map' => $papaiaGroupMap,
	] ] : [],
];

## ── OAuth: per-user sign-in for the MCP server ────────────────────────────
# The MCP server signs each user in through one OAuth 2.0 consumer that the
# entrypoint creates (EnsureOAuthConsumer.php). Every MCP call then runs as
# that user, so the access tiers above apply unchanged. Tokens carry the
# user's wiki identity, not their Keycloak groups: groups follow Keycloak
# only on the next browser login to the wiki (see README, "Access tiers").
wfLoadExtension( 'OAuth' );
$wgMWOAuthCentralWiki = false;
# Only redirects OAuth 1.0a token requests to https; the wiki may run on
# plain http behind a TLS-terminating proxy or on a local test host.
$wgMWOAuthSecureTokenTransfer = str_starts_with( $wgServer, 'https://' );
# Signing key pair for OAuth 2.0 access tokens, generated on first start and
# kept on a volume so tokens survive a container recreate.
$wgOAuth2PrivateKey = '/var/lib/papaia-mediawiki-oauth/oauth2.key';
$wgOAuth2PublicKey = '/var/lib/papaia-mediawiki-oauth/oauth2.pub';
# Consumers are managed by administrators only; every user may review and
# revoke what they authorized (Special:OAuthManageMyGrants).
$wgGroupPermissions['user']['mwoauthproposeconsumer'] = false;
$wgGroupPermissions['user']['mwoauthupdateownconsumer'] = false;
$wgGroupPermissions['user']['mwoauthmanagemygrants'] = true;
foreach ( [ 'mwoauthproposeconsumer', 'mwoauthupdateownconsumer', 'mwoauthmanageconsumer' ] as $papaiaRight ) {
	$wgGroupPermissions['sysop'][$papaiaRight] = true;
}

## ── Misc ──────────────────────────────────────────────────────────────────
$wgShowExceptionDetails = papaiaEnvBool( 'MW_DEBUG', false );
$wgRawHtml = false;
$wgDiff3 = '/usr/bin/diff3';
$wgPingback = false;

## ── Site-specific additions ───────────────────────────────────────────────
foreach ( glob( '/etc/papaia-mediawiki/settings.d/*.php' ) ?: [] as $papaiaExtra ) {
	require_once $papaiaExtra;
}
