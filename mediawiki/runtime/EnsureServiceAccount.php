<?php
/**
 * papaia-addon-mediawiki — the service account other add-ons sign in as, and
 * its bot password, kept equal to the add-on .env.
 *
 * The account is created without a main password, so the bot password is the
 * only way in; its groups are added on every run. Setting a main password
 * (createAndPromote) would invalidate every bot password of the account.
 *
 * The password is read from MW_SERVICE_PASSWORD, never from argv, and never
 * printed (the core's createBotPassword.php echoes it). An unchanged password
 * and grant list leave the row alone, so running sessions survive a restart;
 * a changed one is written over the old, which stops working right away.
 * Without MW_SERVICE_PASSWORD the account's bot password is removed.
 *
 * Bot passwords are otherwise off for everyone: LocalSettings.php removes
 * Special:BotPasswords, so this is the only one.
 */

use MediaWiki\Maintenance\Maintenance;
use MediaWiki\MediaWikiServices;
use MediaWiki\User\BotPassword;
use MediaWiki\User\User;

require_once getenv( 'MW_INSTALL_PATH' ) . '/maintenance/Maintenance.php';

class EnsureServiceAccount extends Maintenance {
	public function __construct() {
		parent::__construct();
		$this->addDescription( 'Create the service account and create, update or remove its bot password' );
		$this->addOption( 'user', 'Wiki user the bot password belongs to', true, true );
		$this->addOption( 'groups', 'Comma-separated groups the account is added to', true, true );
		$this->addOption( 'appid', 'Bot password app id (the part after @ in the login name)', true, true );
		$this->addOption( 'grants', 'Comma-separated grant names', true, true );
	}

	public function execute() {
		$services = MediaWikiServices::getInstance();
		$name = $this->getOption( 'user' );
		$appId = $this->getOption( 'appid' );
		$password = (string)getenv( 'MW_SERVICE_PASSWORD' );

		$user = $services->getUserIdentityLookup()->getUserIdentityByName( $name );
		$current = $user && $user->isRegistered()
			? BotPassword::newFromUser( $user, $appId, IDBAccessObject::READ_LATEST )
			: null;

		if ( $password === '' ) {
			if ( $current ) {
				$current->delete();
				$this->output( "removed the bot password of $name\n" );
			}
			return;
		}
		if ( strlen( $password ) < BotPassword::PASSWORD_MINLENGTH ) {
			$this->fatalError( 'MW_SERVICE_PASSWORD needs at least ' . BotPassword::PASSWORD_MINLENGTH . ' characters' );
		}

		$grants = array_values( array_unique( array_filter( array_map( 'trim', explode( ',', $this->getOption( 'grants' ) ) ) ) ) );
		sort( $grants );
		$invalid = array_diff( $grants, $services->getGrantsInfo()->getValidGrants() );
		if ( $invalid ) {
			$this->fatalError( 'Unknown grants: ' . implode( ', ', $invalid ) );
		}

		if ( !$user || !$user->isRegistered() ) {
			$user = User::createNew( $name );
			if ( !$user ) {
				$this->fatalError( "Could not create the user $name" );
			}
			$this->output( "created the user $name\n" );
		}
		$groupManager = $services->getUserGroupManager();
		$missing = array_diff(
			array_filter( array_map( 'trim', explode( ',', $this->getOption( 'groups' ) ) ) ),
			$groupManager->getUserGroups( $user )
		);
		if ( $missing ) {
			$groupManager->addUserToMultipleGroups( $user, $missing );
			$this->output( "added $name to " . implode( ', ', $missing ) . "\n" );
		}
		$factory = $services->getPasswordFactory();
		if ( $current ) {
			$currentGrants = $current->getGrants();
			sort( $currentGrants );
			$stored = $services->getBotPasswordStore()->getPrimaryDatabase()->newSelectQueryBuilder()
				->select( 'bp_password' )
				->from( 'bot_passwords' )
				->where( [ 'bp_user' => $current->getUserCentralId(), 'bp_app_id' => $appId ] )
				->caller( __METHOD__ )
				->fetchField();
			if ( $currentGrants === $grants && $stored !== false
				&& $factory->newFromCiphertext( $stored )->verify( $password )
			) {
				$this->output( "bot password of $name is current\n" );
				return;
			}
			$current->delete();
		}

		$bp = BotPassword::newUnsaved( [ 'user' => $user, 'appId' => $appId, 'grants' => $grants ] );
		$status = $bp ? $bp->save( 'insert', $factory->newFromPlaintext( $password ) ) : null;
		if ( !$status || !$status->isGood() ) {
			$this->fatalError( 'Could not save the bot password of ' . $name );
		}
		$this->output( "set the bot password of $name\n" );
	}
}

$maintClass = EnsureServiceAccount::class;
require_once RUN_MAINTENANCE_IF_MAIN;
