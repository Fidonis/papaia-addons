<?php
/**
 * papaia-addon-mediawiki — create or update a bot password to a given value.
 *
 * maintenance/createBotPassword.php only creates: it fails once the bot
 * password exists, and it generates the secret itself. The add-on needs the
 * opposite -- the secret is generated on install into the add-on .env, which
 * the MCP server reads too, so the wiki has to follow that value on every
 * start (including after the operator rotates it).
 *
 * The secret is read from an environment variable, never from argv, so it
 * does not show up in the process list.
 */

use MediaWiki\Maintenance\Maintenance;
use MediaWiki\MediaWikiServices;
use MediaWiki\User\BotPassword;

require_once getenv( 'MW_INSTALL_PATH' ) . '/maintenance/Maintenance.php';

class EnsureBotPassword extends Maintenance {
	public function __construct() {
		parent::__construct();
		$this->addDescription( 'Create or update a bot password from an environment variable' );
		$this->addOption( 'user', 'Wiki user that owns the bot password', true, true );
		$this->addOption( 'appid', 'Bot password name (the part after @)', true, true );
		$this->addOption( 'grants', 'Comma-separated grant names', true, true );
		$this->addOption( 'password-env', 'Environment variable holding the secret', true, true );
	}

	public function execute() {
		$services = MediaWikiServices::getInstance();
		$secret = getenv( $this->getOption( 'password-env' ) );
		if ( $secret === false || strlen( $secret ) < 16 ) {
			$this->fatalError( 'The bot password must be set and at least 16 characters long.' );
		}

		$user = $services->getUserIdentityLookup()->getUserIdentityByName( $this->getOption( 'user' ) );
		if ( !$user || !$user->isRegistered() ) {
			$this->fatalError( 'No such user: ' . $this->getOption( 'user' ) );
		}

		$grants = array_values( array_filter( array_map( 'trim', explode( ',', $this->getOption( 'grants' ) ) ) ) );
		$invalid = array_diff( $grants, $services->getGrantsInfo()->getValidGrants() );
		if ( $invalid ) {
			$this->fatalError( 'Unknown grants: ' . implode( ', ', $invalid ) );
		}

		$appId = $this->getOption( 'appid' );
		$store = $services->getBotPasswordStore();
		$password = $services->getPasswordFactory()->newFromPlaintext( $secret );
		$existing = $store->getByUser( $user, $appId, IDBAccessObject::READ_LATEST );

		$bp = BotPassword::newUnsaved( [
			'user' => $user,
			'appId' => $appId,
			'grants' => $grants,
		] );
		if ( $existing ) {
			$status = $store->updateBotPassword( $bp, $password );
			$verb = 'Updated';
		} else {
			$status = $store->insertBotPassword( $bp, $password );
			$verb = 'Created';
		}
		if ( !$status->isGood() ) {
			$this->fatalError( $status );
		}
		$this->output( "$verb bot password {$user->getName()}@$appId\n" );
	}
}

$maintClass = EnsureBotPassword::class;
require_once RUN_MAINTENANCE_IF_MAIN;
