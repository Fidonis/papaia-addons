<?php
/**
 * papaia-addon-mediawiki — ensure the OAuth 2.0 consumer the MCP server signs
 * users in through, and hand its credentials to the MCP server.
 *
 * The OAuth extension's own createOAuthConsumer.php only creates OAuth 1.0a
 * consumers, and a second run creates a second consumer. This script is
 * idempotent: the consumer's version is derived from its callback URL, so the
 * same callback finds the same consumer on every start, and a changed public
 * URL proposes a new one and disables the old ones.
 *
 * The consumer is confidential, uses the authorization-code and refresh-token
 * grants, and is approved right away. Its key and secret are written as a
 * shell-sourceable file the MCP container reads at start; they are never
 * printed.
 */

use MediaWiki\Context\RequestContext;
use MediaWiki\Extension\OAuth\Backend\Consumer;
use MediaWiki\Extension\OAuth\Backend\Utils;
use MediaWiki\Extension\OAuth\Control\ConsumerSubmitControl;
use MediaWiki\Maintenance\Maintenance;
use MediaWiki\MediaWikiServices;

require_once getenv( 'MW_INSTALL_PATH' ) . '/maintenance/Maintenance.php';

class EnsureOAuthConsumer extends Maintenance {
	private const NAME = 'papaia-mcp';

	public function __construct() {
		parent::__construct();
		$this->addDescription( 'Create or reuse the MCP server OAuth 2.0 consumer' );
		$this->requireExtension( 'OAuth' );
		$this->addOption( 'user', 'Wiki user that owns the consumer (needs mwoauthmanageconsumer)', true, true );
		$this->addOption( 'callback', 'OAuth callback URL of the MCP server', true, true );
		$this->addOption( 'grants', 'Comma-separated grant names', true, true );
		$this->addOption( 'email', 'Address set on the owner when it has none', true, true );
		$this->addOption( 'output', 'File the credentials are written to', true, true );
	}

	public function execute() {
		$services = MediaWikiServices::getInstance();
		$user = $services->getUserFactory()->newFromName( $this->getOption( 'user' ) );
		if ( !$user || !$user->isRegistered() ) {
			$this->fatalError( 'No such user: ' . $this->getOption( 'user' ) );
		}
		# Proposing a consumer requires a confirmed address on the owner.
		if ( $user->getEmail() === '' || !$user->isEmailConfirmed() ) {
			$user->setEmail( $user->getEmail() ?: $this->getOption( 'email' ) );
			$user->confirmEmail();
			$user->saveSettings();
		}

		$grants = array_values( array_filter( array_map( 'trim', explode( ',', $this->getOption( 'grants' ) ) ) ) );
		$invalid = array_diff( $grants, $services->getGrantsInfo()->getValidGrants() );
		if ( $invalid ) {
			$this->fatalError( 'Unknown grants: ' . implode( ', ', $invalid ) );
		}

		$callback = $this->getOption( 'callback' );
		# A composer-style version, as the OAuth extension requires.
		$version = '1.' . crc32( $callback );
		$context = RequestContext::getMain();
		$context->setUser( $user );
		$dbw = Utils::getCentralDB( DB_PRIMARY );
		$ownerId = Utils::getCentralIdFromLocalUser( $user );

		$cmr = Consumer::newFromNameVersionUser( $dbw, self::NAME, $version, $ownerId );
		if ( !$cmr ) {
			$status = ( new ConsumerSubmitControl( $context, [
				'action' => 'propose',
				'name' => self::NAME,
				'version' => $version,
				'description' => 'Sign-in for the MediaWiki MCP server (papaia-addon-mediawiki)',
				'callbackUrl' => $callback,
				'callbackIsPrefix' => false,
				'oauthVersion' => Consumer::OAUTH_VERSION_2,
				'oauth2IsConfidential' => true,
				'oauth2GrantTypes' => [ 'authorization_code', 'refresh_token' ],
				'grants' => json_encode( $grants ),
				'granttype' => 'normal',
				'ownerOnly' => false,
				'email' => $user->getEmail(),
				'wiki' => '*',
				'rsaKey' => '',
				'agreement' => true,
				'restrictions' => MWRestrictions::newDefault(),
				# The extension warns about a callback on the wiki's own host,
				# assuming a mistake. Here it is the point: the MCP server is
				# published under the wiki's origin.
				'ignorewarnings' => true,
			], $dbw ) )->submit();
			if ( !$status->isGood() ) {
				$this->fatalError( 'Proposing the consumer failed: ' . $status->getMessage()->text() );
			}
			$cmr = $status->value['result']['consumer'];
			$this->output( "Proposed OAuth consumer " . self::NAME . " $version\n" );
		}
		# Only a fresh proposal is approved here. A consumer an administrator
		# disabled stays disabled across restarts.
		$stage = (int)$cmr->getStage();
		if ( $stage === Consumer::STAGE_PROPOSED ) {
			$this->submit( $context, $dbw, $cmr, 'approve' );
			$this->output( "Approved OAuth consumer " . self::NAME . " $version\n" );
		} elseif ( $stage !== Consumer::STAGE_APPROVED ) {
			$this->output( "WARNING: OAuth consumer " . self::NAME . " $version is "
				. Consumer::$stageNames[$stage] . "; MCP sign-in stays off until it is approved again\n" );
		}

		# Consumers left over from an earlier public URL.
		$stale = $dbw->newSelectQueryBuilder()
			->select( 'oarc_consumer_key' )
			->from( 'oauth_registered_consumer' )
			->where( [
				'oarc_name' => self::NAME,
				'oarc_user_id' => $ownerId,
				'oarc_stage' => Consumer::STAGE_APPROVED,
				$dbw->expr( 'oarc_version', '!=', $version ),
			] )
			->caller( __METHOD__ )
			->fetchFieldValues();
		foreach ( $stale as $key ) {
			$this->submit( $context, $dbw, Consumer::newFromKey( $dbw, $key ), 'disable' );
			$this->output( "Disabled stale OAuth consumer $key\n" );
		}

		$secret = Utils::hmacDBSecret( $cmr->getSecretKey() );
		$file = $this->getOption( 'output' );
		$tmp = "$file.tmp";
		$written = file_put_contents( $tmp, sprintf(
			"WIKI_OAUTH_CLIENT_ID='%s'\nWIKI_OAUTH_CLIENT_SECRET='%s'\n",
			$cmr->getConsumerKey(), $secret
		) );
		if ( $written === false || !chmod( $tmp, 0644 ) || !rename( $tmp, $file ) ) {
			$this->fatalError( "Cannot write $file" );
		}
	}

	private function submit( $context, $dbw, Consumer $cmr, string $action ): void {
		$status = ( new ConsumerSubmitControl( $context, [
			'action' => $action,
			'consumerKey' => $cmr->getConsumerKey(),
			'reason' => 'papaia-addon-mediawiki bootstrap',
			'changeToken' => $cmr->getChangeToken( $context ),
			'suppress' => 0,
		], $dbw ) )->submit();
		if ( !$status->isGood() ) {
			$this->fatalError( "OAuth consumer $action failed: " . $status->getMessage()->text() );
		}
	}
}

$maintClass = EnsureOAuthConsumer::class;
require_once RUN_MAINTENANCE_IF_MAIN;
