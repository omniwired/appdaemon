import asyncio
from appdaemon.appdaemon import AppDaemon

class AdminLoop:

    def __init__(self, ad: AppDaemon):

        self.AD = ad
        self.stopping = False
        self.logger = ad.logging.get_child("_admin_loop")

    def stop(self):
        self.logger.debug("stop() called for admin_loop")
        self.stopping = True

    async def loop(self):
        old_update = {}
        while not self.stopping:
            #update = {}
            #threads = {}
            if self.AD.http.stats_update != "none" and self.AD.sched is not None:
                await self.AD.threading.get_callback_update()

            await asyncio.sleep(self.AD.admin_delay)

