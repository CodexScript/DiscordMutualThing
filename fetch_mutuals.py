import argparse
import discord
import asyncio
import platform
import aiohttp
import config_handler
from sys import exit
from tqdm import trange

parser = argparse.ArgumentParser(description='Mutual Discord Bot')
parser.add_argument('-g', type=str, help='Discord Guild ID', required=False, dest='guild_id')
cli_args = parser.parse_args()

if cli_args.guild_id is None:
    parser.print_usage()
    exit(1)

config = config_handler.get_config()

if config is None:
    print('Creating config.json')
    config_handler.create_config()
    exit(1)

if 'token' not in config:
    print('Please set your token in config.json')
    exit(1)

if 'webhookURL' not in config:
    print('Please set your webhook URL in config.json')
    exit(1)

if 'showAscii' not in config:
    config['showAscii'] = False

if platform.system() == 'Windows':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

is_logged_out = input(
    'To minimize risk of your main account being banned, you should have your Discord desktop app closed. '
    'This is superstition but better safe than sorry. Is it closed currently? (y/n)\n> ')
if is_logged_out.casefold() != 'y'.casefold():
    exit(1)

client = discord.Client(guild_subscription_options=discord.GuildSubscriptionOptions.disabled(),
                        chunk_guilds_at_startup=False)


@client.event
async def on_ready():
    session = aiohttp.ClientSession()

    async def send_webhook(*args, **kwargs):
        webhook = discord.Webhook.from_url(url=config['webhookURL'], adapter=discord.AsyncWebhookAdapter(session))
        await webhook.send(*args, **kwargs)
        await asyncio.sleep(0.25)  # Avoid rate limit

    if config['showAscii']:
        config_handler.print_ripbozo()

        print('(^ me smoking that discord token pack)')
        print('NOW SCRAPING ALL TOKENS FOR EVERYTHING INSTALLED ON THIS COMPUTER')
        print('NEGAR NEGAR GET OUT OF CHINA')

    try:
        print('------')
        print('Logged in as', client.user)
        print('------')
        guild = client.get_guild(int(cli_args.guild_id))
        print('Guild:', guild.name)
        print('------')

        print('Subscribing to guild (this can take some time depending on the member count...)')
        success = await guild.subscribe()
        if success:
            print('Now fetching relationships for each guild member...')
            embeds = []
            progress_bar = trange(len(guild.members))
            for i in progress_bar:
                member = guild.members[i]
                if member.id == client.user.id or member.bot:
                    continue
                progress_bar.set_description(f'{member.name}#{member.discriminator}')
                mutual_guilds = await member.mutual_guilds()
                mutual_friends = await member.mutual_friends()

                embed = discord.Embed(title=f'Your shared mutuals with {member.name}#{member.discriminator}',
                                      description=member.mention, color=discord.Color.purple())
                should_send = False

                if len(mutual_guilds) > 0:
                    embed.add_field(name='Mutual Guilds', value="\n".join(
                        [f'{mutual.name} - https://discord.com/channels/{mutual.id}' for mutual in mutual_guilds]),
                                    inline=False)
                    should_send = True
                if len(mutual_friends) > 0:
                    embed.add_field(name='Mutual Friends',
                                    value="\n".join([mutual.mention for mutual in mutual_friends]), inline=False)
                    should_send = True

                if should_send:
                    embeds.append(embed)

            if len(embeds) > 0:
                print('Executing webhooks')
                embed_sum = sum(len(x) for x in embeds)
                if embed_sum > 6000:  # Character limit workaround
                    slices_step = embed_sum // 6000
                    slices = []
                    for i in range(0, len(embeds), slices_step):
                        slices.append(embeds[i:i + slices_step])
                    for i in trange(len(slices)):
                        await send_webhook(embeds=slices[i])
                else:
                    await send_webhook(embeds=embeds)
            else:
                print('Executing webhook')

            print('Done.')

        else:
            print('Failed to subscribe to guild')

    # Close aiohttp session and discord client session no matter what happens
    # This makes sure we log out of discord and also makes sure we aren't trapped in the terminal
    # (async doesn't like Ctrl+C / KeyboardInterrupt)
    finally:
        await session.close()
        await client.close()


client.run(config['token'])
